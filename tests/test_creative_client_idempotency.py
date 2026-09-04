"""The server committed, and the response was lost on the way back.

The browser used to mint a fresh ``client_turn_id`` per HTTP attempt, so this
was the outcome of every dropped connection: the retry looked like a brand new
turn, the director was called (and paid for) a second time, and the user got
their words answered twice. Fixing the browser is only half of it - these tests
pin the other half, the contract the server owes a retry that carries the id
the first attempt used.

1. a repeated ``client_turn_id`` writes no second turn and makes no second
   model call - the recorded reply is replayed, any number of times;
2. the same id sent with *different* words is refused (409,
   CLIENT_TURN_ID_CONTENT_MISMATCH) rather than answered with the reply that
   belongs to the other words - a replay there would silently swallow a turn
   the user meant to have;
3. session creation is idempotent at the *request* level, not only inside the
   session it creates: a retried POST /v1/creative/sessions returns the same
   session instead of opening a second conversation and paying for a second
   opening turn;
4. the key is scoped to its project - the same id in another project is
   another conversation, never a replay across the boundary.

``tests/test_creative_director.py`` covers the happy replay of one repeated
message; everything here is the part that was missing.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from production_domain.models import CreativeSession, CreativeTurn, Project
from sqlalchemy import func, select
from test_creative_director import (
    RICH_IDEA,
    ScriptedDirector,
    _client,
    _rich_turn,
    _start,
    _state,
)

MISMATCH = "CLIENT_TURN_ID_CONTENT_MISMATCH"


def _turn_count(container, session_id: str, client_turn_id: str) -> int:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(CreativeTurn)
                .where(
                    CreativeTurn.session_id == session_id,
                    CreativeTurn.client_turn_id == client_turn_id,
                )
            )
            or 0
        )


def _session_count(container, project_id: str) -> int:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(CreativeSession)
                .where(CreativeSession.project_id == project_id)
            )
            or 0
        )


def _post_message(client: TestClient, session_id: str, **body: Any):  # type: ignore[no-untyped-def]
    return client.post(f"/v1/creative/sessions/{session_id}/messages", json=body)


def test_a_message_retried_after_a_lost_response_is_never_charged_twice(container, project):
    """The whole defect, from the server's side: one turn, one call, N attempts."""

    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA, client_turn_id="idea-1")
        session_id = started["session_id"]
        calls_after_start = len(container.creative_director.model_roles.calls)

        first = _post_message(client, session_id, content="再悬疑一点", client_turn_id="turn-2")
        assert first.status_code == 200, first.text
        calls_after_first = len(container.creative_director.model_roles.calls)
        assert calls_after_first == calls_after_start + 1

        # The reply never reached the browser. It sends the same words under
        # the same id - twice, because the second retry was lost as well.
        replays = [
            _post_message(client, session_id, content="再悬疑一点", client_turn_id="turn-2").json()
            for _ in range(2)
        ]

    assert len(container.creative_director.model_roles.calls) == calls_after_first, (
        "a retry under a recorded client_turn_id must not reach the model at all"
    )
    for replay in replays:
        assert replay["replayed"] is True
        assert replay["message"] == first.json()["message"]
        assert replay["turn_sequence"] == first.json()["turn_sequence"]
        assert replay["brief_revision"] == first.json()["brief_revision"]
        assert "IDEMPOTENT_REPLAY" in replay["reason_codes"]
    # One user turn on the row, not three - and the director's answer to it.
    assert _turn_count(container, session_id, "turn-2") == 1
    with _client(container) as client:
        assert len(_state(client, session_id)["turns"]) == 4


def test_the_same_client_turn_id_with_different_words_is_refused_not_replayed(container, project):
    """A key identifies one message. Different words behind it are a conflict."""

    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA, client_turn_id="idea-1")
        session_id = started["session_id"]
        _post_message(client, session_id, content="再悬疑一点", client_turn_id="turn-2")
        calls = len(container.creative_director.model_roles.calls)

        refused = _post_message(client, session_id, content="改成白天", client_turn_id="turn-2")
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["reason_code"] == MISMATCH
        assert refused.json()["detail"]["client_turn_id"] == "turn-2"

        # Refused before the model, and with nothing written.
        assert len(container.creative_director.model_roles.calls) == calls
        assert len(_state(client, session_id)["turns"]) == 4

        # A new id for the edited words is the way through, and it works.
        accepted = _post_message(client, session_id, content="改成白天", client_turn_id="turn-3")
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["replayed"] is False
        assert len(_state(client, session_id)["turns"]) == 6


def test_a_retried_session_create_returns_the_same_session(container, project):
    """The create's response was lost. The retry must not open a second session."""

    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        first = client.post(
            "/v1/creative/sessions",
            json={"project_id": project.id, "idea": RICH_IDEA, "client_turn_id": "create-1"},
        )
        assert first.status_code == 201, first.text
        calls = len(container.creative_director.model_roles.calls)

        retry = client.post(
            "/v1/creative/sessions",
            json={"project_id": project.id, "idea": RICH_IDEA, "client_turn_id": "create-1"},
        )
        assert retry.status_code == 201, retry.text

    body, retried = first.json(), retry.json()
    assert retried["session_id"] == body["session_id"]
    assert retried["replayed"] is True and body["replayed"] is False
    assert retried["message"] == body["message"]
    assert retried["turn_sequence"] == body["turn_sequence"]
    assert "IDEMPOTENT_REPLAY" in retried["reason_codes"]
    assert len(container.creative_director.model_roles.calls) == calls, (
        "the retried create must not pay for a second opening turn"
    )
    assert _session_count(container, project.id) == 1
    assert _turn_count(container, body["session_id"], "create-1") == 1
    with _client(container) as client:
        assert len(_state(client, body["session_id"])["turns"]) == 2


def test_a_retried_create_carrying_different_words_is_refused(container, project):
    """The create key is the turn key: it cannot be recycled for another idea."""

    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        _start(client, project.id, RICH_IDEA, client_turn_id="create-1")
        refused = client.post(
            "/v1/creative/sessions",
            json={"project_id": project.id, "idea": "完全不同的点子", "client_turn_id": "create-1"},
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["reason_code"] == MISMATCH
    assert _session_count(container, project.id) == 1, "the refused create wrote no session row"


def test_a_create_without_a_client_turn_id_is_still_a_new_session(container, project):
    """Nothing to be idempotent about: two ideas, two conversations."""

    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        first = _start(client, project.id, RICH_IDEA)
        second = _start(client, project.id, RICH_IDEA)
    assert first["session_id"] != second["session_id"]
    assert _session_count(container, project.id) == 2


def test_the_create_key_does_not_reach_across_projects(container, project):
    """Two projects in one browser must never collide on a pending id."""

    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with container.database.session() as session:
        other = Project(title="Episode Two")
        session.add(other)
        session.flush()
        other_id = other.id

    with _client(container) as client:
        mine = _start(client, project.id, RICH_IDEA, client_turn_id="create-1")
        theirs = _start(client, other_id, RICH_IDEA, client_turn_id="create-1")

    assert mine["session_id"] != theirs["session_id"]
    assert theirs["replayed"] is False
    assert _session_count(container, project.id) == 1
    assert _session_count(container, other_id) == 1
