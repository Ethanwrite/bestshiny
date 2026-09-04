"""Two director turns, one session: what happens when the model is slow.

The model call is deliberately outside every transaction - a provider failure
must never leave a user message without a result - so the writing phase has to
assume the world moved while it was thinking. It did not:

* a turn rebuilt the brief from the snapshot it read *before* the model call,
  so a concurrent brief edit was overwritten with no conflict and a 200;
* `_CLOSED_STATUSES` was only {COMPILED, ABANDONED}, so a turn that landed
  after an approval superseded the APPROVED revision and dragged the session
  back to BRIEF_PROPOSED;
* `_write_screenplay` validated nothing under its lock, so a slow redraft
  landing after an approval moved the head past the approved revision, reset
  the session to SCREENPLAY_PROPOSED, and made a second full set of *paid* key
  visuals derivable for one logical approval.

Every test here is `postgres_only`: SQLite serialises transactions and renders
no FOR UPDATE, so the situation under test cannot be constructed there.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from production_domain.models import CreativeAction, CreativeScreenplayRevision, CreativeSession
from sqlalchemy import func, select
from test_creative_director import (
    RICH_IDEA,
    SCREENPLAY,
    ScriptedDirector,
    _approve_brief,
    _client,
    _rich_turn,
    _start,
    _state,
)


class _GatedDirector(ScriptedDirector):
    """A director whose next reply waits on a gate, so a race can be constructed.

    The gate is armed explicitly for exactly one call, because a single test
    also makes ordinary model calls (approving a brief drafts a screenplay) and
    those must not be caught by it.
    """

    def __init__(self, turn_handler=None, screenplay=None):  # type: ignore[no-untyped-def]
        super().__init__(turn_handler, screenplay)
        self._armed = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()

    def arm(self) -> None:
        self.entered.clear()
        self.release.clear()
        self._armed.set()

    async def execute_chat(self, project_id, role, *, messages, parameters=None):  # type: ignore[no-untyped-def]
        if self._armed.is_set():
            self._armed.clear()
            self.entered.set()
            assert self.release.wait(timeout=20), "the racing thread never released the director"
        return await super().execute_chat(project_id, role, messages=messages, parameters=parameters)


def _run(target) -> threading.Thread:  # type: ignore[no-untyped-def]
    thread = threading.Thread(target=target)
    thread.start()
    return thread


@pytest.mark.postgres_only
def test_a_brief_edit_during_a_turn_is_rebased_not_overwritten(container, project) -> None:
    """The lost update: the turn used to write its pre-model snapshot back."""

    director = _GatedDirector(_rich_turn)
    container.creative_director.model_roles = director
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        director.arm()

        replies: list[Any] = []

        def send() -> None:
            with _client(container) as inner:
                replies.append(
                    inner.post(
                        f"/v1/creative/sessions/{session_id}/messages",
                        json={"content": "keep going", "client_turn_id": "race-turn"},
                    )
                )

        thread = _run(send)
        assert director.entered.wait(timeout=20), "the director never reached the model call"
        # The user edits the brief while the director is still thinking.
        edited = client.post(
            f"/v1/creative/sessions/{session_id}/brief/edit",
            json={
                "operations": [
                    {
                        "op": "REPLACE",
                        "path": "platform",
                        "value": "Bilibili",
                        "evidence": "brief editor",
                    }
                ]
            },
        )
        assert edited.status_code == 200, edited.text
        director.release.set()
        thread.join(timeout=30)

        assert replies and replies[0].status_code == 200, replies[0].text
        assert "BRIEF_REBASED" in replies[0].json()["reason_codes"]
        view = _state(client, session_id)

    fields = view["brief"]["fields"]
    # The edit survived the turn that landed on top of it.
    assert fields["platform"] == "Bilibili"
    assert view["brief"]["provenance"]["platform"]["source"] == "USER_EDIT"
    # And the user's own facts from the opening message are still there.
    assert fields["setting"]["location"].startswith("rooftop")


@pytest.mark.postgres_only
def test_an_in_flight_turn_cannot_un_approve_an_approved_brief(container, project) -> None:
    director = _GatedDirector(_rich_turn)
    container.creative_director.model_roles = director
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        revision = started["brief_revision"]
        director.arm()

        replies: list[Any] = []

        def send() -> None:
            with _client(container) as inner:
                replies.append(
                    inner.post(
                        f"/v1/creative/sessions/{session_id}/messages",
                        json={"content": "one more thought", "client_turn_id": "race-approve"},
                    )
                )

        thread = _run(send)
        assert director.entered.wait(timeout=20)
        approved = _approve_brief(client, session_id, revision)
        director.release.set()
        thread.join(timeout=30)

        assert replies and replies[0].status_code == 409, replies[0].text
        assert replies[0].json()["detail"]["reason_code"] == "SESSION_STAGE_CHANGED"
        view = _state(client, session_id)

    # The approval stands, and the head is still the approved revision.
    assert view["brief"]["status"] == "APPROVED"
    assert view["brief"]["revision"] == approved["approved_revision"]
    assert view["session"]["status"] != "BRIEF_PROPOSED"
    assert view["session"]["brief_revision"] == approved["approved_revision"]


@pytest.mark.postgres_only
def test_a_client_that_pins_a_stale_brief_revision_is_told_so(container, project) -> None:
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        client.post(
            f"/v1/creative/sessions/{session_id}/brief/edit",
            json={
                "operations": [
                    {"op": "REPLACE", "path": "platform", "value": "Bilibili", "evidence": "editor"}
                ]
            },
        )
        stale = client.post(
            f"/v1/creative/sessions/{session_id}/messages",
            json={"content": "go on", "expected_brief_revision": started["brief_revision"]},
        )
    assert stale.status_code == 409, stale.text
    detail = stale.json()["detail"]
    assert detail["reason_code"] == "BRIEF_REVISION_CHANGED"
    assert detail["retryable"] is True


@pytest.mark.postgres_only
def test_a_slow_redraft_cannot_rewind_an_approved_screenplay(container, project) -> None:
    """The expensive one: a rewind made a second paid key-visual set derivable."""

    director = _GatedDirector(_rich_turn)
    container.creative_director.model_roles = director
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        view = _state(client, session_id)
        assert view["session"]["status"] == "SCREENPLAY_PROPOSED"
        approved_revision = view["screenplay"]["revision"]

        director.arm()
        redrafts: list[Any] = []

        def redraft() -> None:
            with _client(container) as inner:
                redrafts.append(
                    inner.post(
                        f"/v1/creative/sessions/{session_id}/screenplay/propose",
                        json={"notes": "make it funnier"},
                    )
                )

        thread = _run(redraft)
        assert director.entered.wait(timeout=20), "the redraft never reached the model"
        approved = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/approve",
            json={"revision": approved_revision},
        )
        assert approved.status_code == 200, approved.text
        director.release.set()
        thread.join(timeout=30)

        assert redrafts and redrafts[0].status_code == 409, redrafts[0].text
        assert redrafts[0].json()["detail"]["reason_code"] in {
            "SCREENPLAY_STAGE_CHANGED",
            "SCREENPLAY_REVISION_CHANGED",
        }
        view = _state(client, session_id)

    # The state machine did not go backwards.
    assert view["session"]["status"] == "VISUALS_IN_PROGRESS"
    assert view["session"]["screenplay_revision"] == approved_revision
    statuses = [item["status"] for item in view["screenplays"]]
    assert statuses.count("APPROVED") == 1
    assert len(view["screenplays"]) == 1, statuses
    with container.database.session() as session:
        # One approval, one set of paid key-visual actions.
        emitted = session.scalar(
            select(func.count(CreativeAction.id)).where(
                CreativeAction.session_id == session_id,
                CreativeAction.kind == "GENERATE_KEY_VISUAL",
            )
        )
        anchors = len(view["anchors"])
    assert emitted == anchors, (emitted, anchors)


@pytest.mark.postgres_only
def test_a_screenplay_edit_racing_an_approval_is_refused(container, project) -> None:
    """edit_screenplay had the same gap with no model call to blame."""

    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        view = _state(client, session_id)
        revision = view["screenplay"]["revision"]
        approved = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/approve", json={"revision": revision}
        )
        assert approved.status_code == 200, approved.text
        # An edit prepared before the approval, submitted after it.
        content = dict(SCREENPLAY)
        late = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/edit", json={"content": content}
        )
    assert late.status_code == 409, late.text
    assert late.json()["detail"]["reason_code"] == "INVALID_TRANSITION"
    with container.database.session() as session:
        rows = list(
            session.scalars(
                select(CreativeScreenplayRevision).where(
                    CreativeScreenplayRevision.session_id == session_id
                )
            )
        )
        row = session.get(CreativeSession, session_id)
    assert len(rows) == 1 and rows[0].status == "APPROVED"
    assert row.current_screenplay_revision == revision
