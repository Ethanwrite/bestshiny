"""The creative director: dialogue, brief, key visuals, bible lock, beats.

What these tests pin, one per defect class:

1. questions come from gap analysis, never a fixed questionnaire - a rich
   idea is asked nothing, a vague one is asked at most three high-value
   questions, and an asked question is never repeated;
2. approving the brief emits structured GENERATE_KEY_VISUAL actions and the
   API executes them through the existing Passenger admission path - real
   GenerationJob rows, idempotent on replay;
3. a completed image job binds to its anchor through sync, via the ordinary
   Gateway completion path;
4. the visual bible is versioned and a LOCKED version is immutable;
5. approving beats compiles a real episode through the existing narrative
   compiler, applies the structured shot intents, and opens the cliffhanger
   obligation in the series ledger;
6. model reasoning degrades to the deterministic engine loudly (recorded
   reasoner and reason codes), never silently.
"""

from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from production_domain.models import (
    CreativeAction,
    CreativeSession,
    CreativeVisualAnchor,
    Episode,
    GenerationJob,
    NarrativeObligation,
    Scene,
    Shot,
    VisualBibleVersion,
)
from provider_sdk.transport import MockProviderTransport, ProviderHttpResponse
from sqlalchemy import select
from video_platform_api.main import create_app

RICH_IDEA = (
    "I want a 30 second suspenseful short drama on TikTok, vertical 9:16, "
    "cinematic live-action, protagonist is Mira, set in rooftop at night. "
    "She finds a phone that is not hers."
)
VAGUE_IDEA = "帮我做一个短剧"


def _client(container) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(create_app(container))


def _start(client: TestClient, project_id: str, idea: str) -> dict:
    response = client.post(
        "/v1/creative/sessions", json={"project_id": project_id, "idea": idea}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_vague_idea_asks_only_high_value_questions_and_never_repeats(container, project):
    with _client(container) as client:
        started = _start(client, project.id, VAGUE_IDEA)
        assert started["status"] == "CLARIFYING"
        first_questions = started["questions"]
        assert 1 <= len(first_questions) <= 3
        first_codes = {question["code"] for question in first_questions}
        # The format was stated ("短剧"), so the director must not ask for it.
        assert "FORMAT" not in first_codes

        reply = client.post(
            f"/v1/creative/sessions/{started['session_id']}/messages",
            json={"content": "就叫《雨夜》，主角是雨桐，在天台，大概60秒，悬疑一点"},
        )
        assert reply.status_code == 200, reply.text
        second_codes = {question["code"] for question in reply.json()["questions"]}
        assert not (first_codes & second_codes), "an asked question was asked again"


def test_rich_idea_needs_no_questions_and_proposes_a_brief(container, project):
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        assert started["questions"] == []
        assert started["proposable"] is True
        assert started["status"] == "BRIEF_PROPOSED"
        # Mock mode has no reachable DIRECTOR model; the degradation is
        # recorded, never silent.
        assert started["reasoner"] == "DETERMINISTIC"

        state = client.get(f"/v1/creative/sessions/{started['session_id']}").json()
        fields = state["brief"]["fields"]
        assert fields["format"] == "SHORT_DRAMA"
        assert fields["duration_seconds"] == 30
        assert fields["aspect_ratio"] == "9:16"
        assert fields["characters"][0]["name"] == "Mira"
        assert fields["setting"]["location"].lower().startswith("rooftop")


def test_brief_approval_emits_and_executes_key_visual_actions(openrouter_container):
    container = openrouter_container
    from production_domain.models import Project

    with container.database.session() as session:
        project = Project(title="Key Visuals")
        session.add(project)
        session.flush()
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        approved = client.post(
            f"/v1/creative/sessions/{session_id}/brief/approve",
            json={"revision": started["brief_revision"]},
        )
        assert approved.status_code == 200, approved.text
        body = approved.json()
        kinds = {action["kind"] for action in body["actions"]}
        assert kinds == {"GENERATE_KEY_VISUAL"}
        executed = [entry for entry in body["executions"] if entry["status"] == "EXECUTED"]
        assert len(executed) == len(body["executions"]) >= 3  # character, scene, style

        with container.database.session() as session:
            jobs = list(session.scalars(select(GenerationJob)))
            assert len(jobs) == len(executed)
            assert {job.generation_type for job in jobs} == {"image"}
            anchors = list(session.scalars(select(CreativeVisualAnchor)))
            assert {anchor.status for anchor in anchors} == {"GENERATING"}
            assert all(anchor.generation_job_id for anchor in anchors)
            character_anchor = next(a for a in anchors if a.kind == "CHARACTER")
            assert character_anchor.character_id is not None

        # Replaying the executor creates nothing new: the actions are already
        # EXECUTED and the idempotency keys already claimed.
        replay = client.post(f"/v1/creative/sessions/{session_id}/visuals/execute")
        assert replay.status_code == 200
        assert replay.json()["executions"] == []
        with container.database.session() as session:
            assert len(list(session.scalars(select(GenerationJob)))) == len(executed)

            # A failed action remains retryable.  Its idempotency key reuses
            # the existing job instead of spending or inserting twice.
            action = session.scalar(select(CreativeAction).order_by(CreativeAction.sequence))
            action.status = "FAILED"
            retry_anchor_key = action.payload_json["anchor_key"]
            anchor = session.scalar(
                select(CreativeVisualAnchor).where(
                    CreativeVisualAnchor.anchor_key == retry_anchor_key
                )
            )
            anchor.status = "FAILED"
            anchor.failure_code = "TRANSIENT_TEST_FAILURE"
        retried = client.post(f"/v1/creative/sessions/{session_id}/visuals/execute")
        assert retried.status_code == 200
        assert len(retried.json()["executions"]) == 1
        assert retried.json()["executions"][0]["status"] == "EXECUTED"
        with container.database.session() as session:
            assert len(list(session.scalars(select(GenerationJob)))) == len(executed)
            anchor = session.scalar(
                select(CreativeVisualAnchor).where(
                    CreativeVisualAnchor.anchor_key == retry_anchor_key
                )
            )
            assert anchor.status == "GENERATING"
            assert anchor.failure_code is None


def test_bible_lock_is_versioned_and_immutable(container, project):
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        client.post(
            f"/v1/creative/sessions/{session_id}/brief/approve",
            json={"revision": started["brief_revision"]},
        )
        proposed = client.post(f"/v1/creative/sessions/{session_id}/bible/propose")
        assert proposed.status_code == 200, proposed.text
        version_one = proposed.json()["version"]

        # A second proposal before locking supersedes the first draft.
        second = client.post(f"/v1/creative/sessions/{session_id}/bible/propose").json()
        assert second["version"] == version_one + 1

        stale = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve", json={"version": version_one}
        )
        assert stale.status_code == 409

        locked = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve", json={"version": second["version"]}
        )
        assert locked.status_code == 200
        assert locked.json()["status"] == "LOCKED"
        assert locked.json()["locked_at"] is not None

        # Locking is idempotent; proposing after the lock is refused - a
        # locked bible is the version lock the product promises.
        again = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve", json={"version": second["version"]}
        )
        assert again.status_code == 200
        refused = client.post(f"/v1/creative/sessions/{session_id}/bible/propose")
        assert refused.status_code == 409

        with container.database.session() as session:
            statuses = {
                bible.version: bible.status
                for bible in session.scalars(select(VisualBibleVersion))
            }
            assert statuses == {version_one: "SUPERSEDED", second["version"]: "LOCKED"}


def test_beats_approval_compiles_an_episode_through_the_existing_chain(container, project):
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        client.post(
            f"/v1/creative/sessions/{session_id}/brief/approve",
            json={"revision": started["brief_revision"]},
        )
        bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose").json()
        client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve", json={"version": bible["version"]}
        )
        proposed = client.post(f"/v1/creative/sessions/{session_id}/beats/propose")
        assert proposed.status_code == 200, proposed.text
        beats = proposed.json()["beats"]
        assert [beat["intent"] for beat in beats][0] == "COLD_OPEN"
        assert any(beat["intent"] == "CLIFFHANGER" for beat in beats)

        confirmed = client.post(
            f"/v1/creative/sessions/{session_id}/beats/approve",
            json={"plan_revision": 1},
        )
        assert confirmed.status_code == 200, confirmed.text
        result = confirmed.json()
        assert result["status"] == "COMPILED"
        shot_ids = result["shot_ids"]
        assert shot_ids

        with container.database.session() as session:
            episode = session.get(Episode, result["episode_id"])
            assert episode is not None and episode.status == "COMPILED"
            scenes = list(session.scalars(select(Scene).where(Scene.episode_id == episode.id)))
            assert scenes
            shots = [session.get(Shot, shot_id) for shot_id in shot_ids]
            assert all(shot is not None for shot in shots)
            # The structured intents reached the real rows: the plan's WIDE
            # opening survives, and dialogue beats keep the compiler's type.
            assert shots[0].shot_type == "WIDE"
            assert any(shot.shot_type == "DIALOGUE" for shot in shots)
            obligation = session.scalar(
                select(NarrativeObligation).where(NarrativeObligation.project_id == project.id)
            )
            assert obligation is not None and obligation.status == "OPEN"
            assert "creative:" in obligation.obligation_key

        # Approving again replays the same compiled episode - no duplicate.
        replay = client.post(
            f"/v1/creative/sessions/{session_id}/beats/approve",
            json={"plan_revision": 1},
        )
        assert replay.status_code == 200
        assert replay.json()["episode_id"] == result["episode_id"]
        with container.database.session() as session:
            assert (
                len(list(session.scalars(select(Episode).where(Episode.project_id == project.id))))
                == 1
            )


def test_beats_compile_retry_reuses_the_persisted_episode(container, project, monkeypatch):
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        client.post(
            f"/v1/creative/sessions/{session_id}/brief/approve",
            json={"revision": started["brief_revision"]},
        )
        bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose").json()
        client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            json={"version": bible["version"]},
        )
        client.post(f"/v1/creative/sessions/{session_id}/beats/propose")

    compiler = container.creative_director.orchestrator
    original_compile = compiler.compile_episode

    def fail_after_episode_persisted(_episode_id: str):
        raise RuntimeError("transient compiler failure")

    monkeypatch.setattr(compiler, "compile_episode", fail_after_episode_persisted)
    with pytest.raises(RuntimeError, match="transient compiler failure"):
        container.creative_director.approve_beats(
            session_id, plan_revision=1, actor="test"
        )
    with container.database.session() as session:
        row = session.get(CreativeSession, session_id)
        persisted_episode_id = row.compiled_episode_id
        assert persisted_episode_id is not None
        assert len(list(session.scalars(select(Episode)))) == 1

    monkeypatch.setattr(compiler, "compile_episode", original_compile)
    result = container.creative_director.approve_beats(
        session_id, plan_revision=1, actor="test"
    )
    assert result["episode_id"] == persisted_episode_id
    with container.database.session() as session:
        assert len(list(session.scalars(select(Episode)))) == 1


def test_dialogue_is_closed_after_compile(container, project):
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        client.post(
            f"/v1/creative/sessions/{session_id}/brief/approve",
            json={"revision": started["brief_revision"]},
        )
        bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose").json()
        client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve", json={"version": bible["version"]}
        )
        client.post(f"/v1/creative/sessions/{session_id}/beats/propose")
        client.post(
            f"/v1/creative/sessions/{session_id}/beats/approve", json={"plan_revision": 1}
        )
        closed = client.post(
            f"/v1/creative/sessions/{session_id}/messages", json={"content": "change everything"}
        )
        assert closed.status_code == 409


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


@pytest.mark.asyncio
async def test_visual_sync_binds_the_completed_job_to_its_anchor(openrouter_container):
    from model_registry_core import ModelRole
    from openrouter_provider import OpenRouterProvider
    from production_domain.models import BrowserWorker, Project, ProviderAccount

    container = openrouter_container
    resolved = container.model_infrastructure.resolve_role(ModelRole.IMAGE_GENERATION)
    provider = container.providers.get(resolved.provider)
    assert isinstance(provider, OpenRouterProvider)
    provider.client.transport = MockProviderTransport({("POST", "/images"): _png_response()})

    with container.database.session() as session:
        project = Project(title="Creative Visuals")
        session.add(project)
        session.flush()
        account = ProviderAccount(
            provider=resolved.provider,
            account_identifier="openrouter@example.com",
            tier="PRO",
            credits=100,
            image_capacity=2,
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
            max_jobs=2,
        )
        session.add(worker)
        account.worker_id = worker.id
        session.flush()
        project_id = project.id

    with _client(container) as client:
        started = _start(client, project_id, RICH_IDEA)
        session_id = started["session_id"]
        approved = client.post(
            f"/v1/creative/sessions/{session_id}/brief/approve",
            json={"revision": started["brief_revision"]},
        ).json()
        job_ids = [entry["job_id"] for entry in approved["executions"]]
        assert job_ids

        for job_id in job_ids:
            completed = await container.gateway.process(job_id)
            assert completed.status == "COMPLETED"

        synced = client.post(f"/v1/creative/sessions/{session_id}/visuals/sync").json()
        assert synced["all_terminal"] is True
        assert synced["ready"] == len(job_ids)
        assert all(anchor["media_asset_id"] for anchor in synced["anchors"])


def test_session_rows_are_the_audit_of_everything_that_happened(container, project):
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        client.post(
            f"/v1/creative/sessions/{session_id}/brief/approve",
            json={"revision": started["brief_revision"]},
        )
        state = client.get(f"/v1/creative/sessions/{session_id}").json()
        assert state["session"]["status"] == "VISUALS_IN_PROGRESS"
        assert [turn["speaker"] for turn in state["turns"]] == ["USER", "DIRECTOR"]
        assert all(
            action["kind"] == "GENERATE_KEY_VISUAL" and action["idempotency_key"]
            for action in state["actions"]
        )
    with container.database.session() as session:
        row = session.get(CreativeSession, session_id)
        assert row is not None and row.workspace_id == project.workspace_id
        actions = list(session.scalars(select(CreativeAction)))
        assert all(action.status in {"EXECUTED", "FAILED"} for action in actions)


# --- 2026-09-02: the director speaks, remembers, and can be dismissed ------


class _TalkingReasoner:
    """A DIRECTOR model that answers with its own words plus a field patch."""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def execute_chat(self, project_id, role, *, messages, parameters=None):  # type: ignore[no-untyped-def]
        del project_id, role, parameters
        self.calls.append(list(messages))
        turn = len(self.calls)
        payload = {
            "reply": f"明白了，这是第{turn}轮：我们把天台的雨夜做成冷暖对撞的悬疑短剧。",
            "fields": {"tone": ["suspense"], "setting": {"location": "rooftop", "time": "night"}},
        }
        import json as _json

        content = _json.dumps(payload, ensure_ascii=False)
        response = {"choices": [{"message": {"content": content}}]}
        return type("Execution", (), {"response": response})()


def test_the_directors_reply_is_the_models_words_and_it_sees_the_conversation(container, project):
    reasoner = _TalkingReasoner()
    container.creative_director.model_roles = reasoner
    with _client(container) as client:
        started = _start(client, project.id, VAGUE_IDEA)
        assert started["reasoner"] == "MODEL:DIRECTOR"
        assert started["message"].startswith("明白了，这是第1轮")
        session_id = started["session_id"]
        second = client.post(
            f"/v1/creative/sessions/{session_id}/messages", json={"content": "主角叫雨桐，30秒竖屏"}
        )
        assert second.status_code == 200, second.text
        assert second.json()["message"].startswith("明白了，这是第2轮")
        view = client.get(f"/v1/creative/sessions/{session_id}").json()
    director_turns = [turn for turn in view["turns"] if turn["speaker"] == "DIRECTOR"]
    assert len(director_turns) == 2
    assert director_turns[0]["content"].startswith("明白了，这是第1轮：")
    assert director_turns[1]["content"].startswith("明白了，这是第2轮：")
    # The second call carried the first exchange as conversation, not only the
    # latest message: system, user(idea), assistant(reply 1), user(latest).
    roles = [message["role"] for message in reasoner.calls[1]]
    assert roles[0] == "system" and roles[-1] == "user"
    assert "assistant" in roles and roles.count("user") >= 2
    assert "明白了，这是第1轮" in reasoner.calls[1][roles.index("assistant")]["content"]


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
    assert started["reasoner"] == "MODEL:DIRECTOR"
    assert started["message"] == "A few things would sharpen this a lot:"


def test_typing_an_approval_approves_the_proposed_brief(container, project):
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        assert started["status"] == "BRIEF_PROPOSED"
        session_id = started["session_id"]
        approved = client.post(f"/v1/creative/sessions/{session_id}/messages", json={"content": "批准。"})
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["reasoner"] == "APPROVAL"
        assert body["approved_revision"] == started["brief_revision"]
        view = client.get(f"/v1/creative/sessions/{session_id}").json()
    assert view["session"]["status"] == "VISUALS_IN_PROGRESS"
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
        view = client.get(f"/v1/creative/sessions/{session_id}").json()
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
        # Still readable as history, closed for dialogue.
        assert client.get(f"/v1/creative/sessions/{first}").json()["session"]["status"] == "ABANDONED"
        refused = client.post(f"/v1/creative/sessions/{first}/messages", json={"content": "还在吗"})
        assert refused.status_code == 409
        assert client.delete(f"/v1/creative/sessions/{first}").status_code == 200
        assert client.delete("/v1/creative/sessions/nope").status_code == 404
