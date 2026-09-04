"""A model cannot promote its own reading to the user's word.

`confidence: "USER_STATED"` was a string the model wrote, and nothing compared
the operation's `evidence` to anything the user had actually said. That single
word was the key to every gate that protects a user fact: REPLACE over an
established value, REMOVE (which also deletes the provenance record of the
deletion), a whole-cast REPLACE, and the KEEP that flips the director's own
assumption to ASSUMPTION_ACCEPTED and satisfies the approval gate. A claimed
`skipped_question_code` needed no corroboration at all and could silence a
CRITICAL question the user was never asked.

These tests forge each of those in turn and pin the demotion.
"""

from __future__ import annotations

from creative_director_core.evidence import UserTextIndex, UserUtterance, normalize
from test_creative_director import (
    RICH_IDEA,
    ScriptedDirector,
    _client,
    _rich_turn,
    _start,
    _state,
)

FORGED_QUOTE = "the client said to move it to a subway platform"


def _turn(client, session_id: str, content: str) -> dict:
    response = client.post(f"/v1/creative/sessions/{session_id}/messages", json={"content": content})
    assert response.status_code == 200, response.text
    return response.json()


def _last_director_turn(view: dict) -> dict:
    return [turn for turn in view["turns"] if turn["speaker"] == "DIRECTOR"][-1]


# --------------------------------------------------------------- the matcher
def test_normalization_ignores_case_spacing_and_punctuation_but_not_wording():
    index = UserTextIndex(
        [
            UserUtterance("t1", 1, "Make it 30 seconds, on a ROOFTOP — at night."),
            UserUtterance("t2", 3, "换到地铁站。别用天台了"),
        ]
    )
    assert index.verify("30 seconds").verified
    assert index.verify("on a rooftop at night").verified
    assert index.verify("换到地铁站").verified
    # Same meaning, different words: not a quote.
    assert not index.verify("half a minute").verified
    assert not index.verify("").verified
    assert normalize("A,  B!") == "a b"


def test_a_fragment_is_not_a_quote():
    """A letter inside a word cannot authorise moving a user fact."""

    index = UserTextIndex(
        [UserUtterance("t1", 1, "Make a short drama set in Tokyo at night, please.")]
    )
    # "a" occurs inside "Make"; "al" would occur inside "always".
    assert not index.verify("a").verified
    assert index.verify("a").reason == "NO_EVIDENCE"
    assert not index.verify("to").verified
    # A word the user actually wrote, aligned to its boundaries, still passes.
    assert index.verify("Tokyo").verified
    assert index.verify("short drama").verified
    # And a mid-word match of a long-enough needle is refused.
    assert not index.verify("leas").verified
    # CJK carries more per character, so its floor is lower.
    cjk = UserTextIndex([UserUtterance("t1", 1, "帮我做一个短剧")])
    assert cjk.verify("短剧").verified
    assert not cjk.verify("剧").verified


def test_a_forged_one_character_quote_cannot_replace_a_user_fact(container, project):
    """The end-to-end shape of the same defect, through the real turn path."""

    def handler(latest: str, state: dict) -> dict:
        if not state.get("brief"):
            return _rich_turn(latest, state)
        return {
            "assistant_message": "Moved.",
            "brief_operations": [
                {
                    "op": "REPLACE",
                    "path": "setting.location",
                    "value": "Paris",
                    "evidence": "a",
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
    reasons = {item["reason"] for item in _last_director_turn(view)["result"]["rejected_operations"]}
    assert "NO_EVIDENCE" in reasons


def test_a_verified_quote_reports_the_turn_and_the_span_in_the_users_own_text():
    index = UserTextIndex([UserUtterance("turn-7", 5, "I want it on a rooftop at night.")])
    verdict = index.verify("on a rooftop")
    assert verdict.verified and verdict.turn_id == "turn-7" and verdict.turn_sequence == 5
    start, end = verdict.span
    assert "I want it on a rooftop at night."[start:end] == "on a rooftop"
    assert verdict.quote == "on a rooftop"


def test_naming_a_turn_that_is_not_on_record_fails_the_proof():
    index = UserTextIndex([UserUtterance("turn-1", 1, "on a rooftop")])
    verdict = index.verify("on a rooftop", turn_id="turn-99")
    assert not verdict.verified and verdict.reason == "EVIDENCE_TURN_NOT_FOUND"


# ------------------------------------------------------------- forged claims
def test_a_forged_user_stated_replace_cannot_overwrite_a_user_fact(container, project):
    """The exact attack: an INFERRED operation relabelled USER_STATED."""

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
                    "evidence": FORGED_QUOTE,
                    "confidence": "USER_STATED",
                },
                {
                    "op": "REPLACE",
                    "path": "duration_seconds",
                    "value": 60,
                    "evidence": "make it a minute",
                    "confidence": "USER_STATED",
                },
            ],
        }

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        reply = _turn(client, session_id, "what would you change?")
        view = _state(client, session_id)

    assert "EVIDENCE_UNVERIFIED" in reply["reason_codes"]
    assert "OPERATIONS_REJECTED" in reply["reason_codes"]
    fields = view["brief"]["fields"]
    assert fields["setting"]["location"] == "rooftop"
    assert fields["duration_seconds"] == 30
    rejected = _last_director_turn(view)["result"]["rejected_operations"]
    demoted = {item["path"]: item for item in rejected if item.get("reason") == "EVIDENCE_NOT_IN_USER_TEXT"}
    assert set(demoted) == {"setting.location", "duration_seconds"}
    assert demoted["setting.location"]["claimed"] == "USER_STATED"
    assert demoted["setting.location"]["recorded"] == "MODEL_INFERRED"
    # And the value the user really did establish still says so.
    assert view["brief"]["provenance"]["setting.location"]["source"] == "USER_STATED"


def test_a_forged_user_stated_remove_cannot_delete_a_user_fact_or_its_provenance(container, project):
    def handler(latest: str, state: dict) -> dict:
        if not state.get("brief"):
            return _rich_turn(latest, state)
        return {
            "assistant_message": "Dropping Mira.",
            "brief_operations": [
                {
                    "op": "REMOVE",
                    "path": "characters",
                    "value": {"name": "Mira"},
                    "evidence": "the client asked to remove Mira",
                    "confidence": "USER_STATED",
                }
            ],
        }

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _turn(client, session_id, "any thoughts?")
        view = _state(client, session_id)

    fields = view["brief"]["fields"]
    assert [member["name"] for member in fields["characters"]] == ["Mira"]
    assert view["brief"]["provenance"]["characters/mira"]["source"] == "USER_STATED"
    reasons = {item["reason"] for item in _last_director_turn(view)["result"]["rejected_operations"]}
    assert "EVIDENCE_NOT_IN_USER_TEXT" in reasons
    assert "REMOVE_REQUIRES_USER_STATEMENT" in reasons


def test_a_forged_user_stated_keep_cannot_confirm_the_directors_own_assumption(container, project):
    """A model-confirmed assumption would satisfy the human approval gate."""

    def handler(latest: str, state: dict) -> dict:
        if not state.get("brief"):
            return _rich_turn(latest, state)
        return {
            "assistant_message": "Locking the palette.",
            "brief_operations": [
                {
                    "op": "SET",
                    "path": "visual_style.palette",
                    "value": "teal and amber",
                    "evidence": "my reading of the reference",
                    "confidence": "INFERRED",
                },
                {
                    "op": "KEEP",
                    "path": "visual_style.palette",
                    "evidence": "the client confirmed teal and amber",
                    "confidence": "USER_STATED",
                },
            ],
        }

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _turn(client, session_id, "go on")
        view = _state(client, session_id)

    record = view["brief"]["provenance"]["visual_style.palette"]
    assert record["source"] == "MODEL_INFERRED", record
    assert record["source"] != "ASSUMPTION_ACCEPTED"


def test_a_genuine_quote_is_honoured_and_records_where_it_was_found(container, project):
    def handler(latest: str, state: dict) -> dict:
        if not state.get("brief"):
            return _rich_turn(latest, state)
        return {
            "assistant_message": "Moved.",
            "brief_operations": [
                {
                    "op": "REPLACE",
                    "path": "setting.location",
                    "value": "subway platform",
                    "evidence": "move it to a subway platform",
                    "confidence": "USER_STATED",
                }
            ],
        }

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _turn(client, session_id, "Actually, move it to a subway platform instead.")
        view = _state(client, session_id)

    assert view["brief"]["fields"]["setting"]["location"] == "subway platform"
    record = view["brief"]["provenance"]["setting.location"]
    assert record["source"] == "USER_STATED"
    verification = record["evidence_verification"]
    assert verification["verified"] is True and verification["reason"] == "QUOTED_BY_USER"
    assert verification["quote"] == "move it to a subway platform"
    assert verification["turn_id"] and verification["span"]


def test_a_quote_from_an_earlier_user_turn_is_honoured(container, project):
    calls: list[int] = []

    def handler(latest: str, state: dict) -> dict:
        calls.append(1)
        if not state.get("brief"):
            return _rich_turn(latest, state)
        if len(calls) == 2:
            return {"assistant_message": "Noted.", "brief_operations": []}
        return {
            "assistant_message": "Applying what you said earlier.",
            "brief_operations": [
                {
                    "op": "REPLACE",
                    "path": "setting.location",
                    "value": "subway platform",
                    "evidence": "a subway platform",
                    "confidence": "USER_STATED",
                }
            ],
        }

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _turn(client, session_id, "I keep picturing a subway platform.")
        _turn(client, session_id, "do that")
        view = _state(client, session_id)

    assert view["brief"]["fields"]["setting"]["location"] == "subway platform"
    verification = view["brief"]["provenance"]["setting.location"]["evidence_verification"]
    assert verification["verified"] is True
    earlier = [turn for turn in view["turns"] if turn["speaker"] == "USER"][1]
    assert verification["turn_id"] == earlier["id"]


# ------------------------------------------------------------- forged skips
def test_a_claimed_skip_of_a_question_that_was_never_asked_is_refused(container, project):
    """The forged skip: silence a CRITICAL gap the user never saw."""

    def handler(latest: str, state: dict) -> dict:
        return {
            "assistant_message": "Understood.",
            "brief_operations": [],
            "skipped_question_codes": ["LOGLINE", "PROTAGONIST"],
            "skipped_questions": [
                {"code": "LOGLINE", "evidence": "the client said skip it"},
                {"code": "PROTAGONIST", "evidence": "the client said skip it"},
            ],
        }

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, "帮我做一个短剧")
        session_id = started["session_id"]
        reply = _turn(client, session_id, "先这样")
        view = _state(client, session_id)

    assert "SKIP_UNVERIFIED" in reply["reason_codes"]
    result = _last_director_turn(view)["result"]
    assert result["skipped_question_codes"] == []
    assert set(result["claimed_skipped_question_codes"]) == {"LOGLINE", "PROTAGONIST"}
    refused = {item["path"]: item["reason"] for item in result["refused_skips"]}
    assert refused, result
    states = view["brief"]["question_states"]
    assert all(state["status"] != "SKIPPED_BY_USER" for state in states.values()), states


def test_a_skip_of_an_asked_question_still_needs_the_users_own_words(container, project):
    calls: list[int] = []

    def handler(latest: str, state: dict) -> dict:
        calls.append(1)
        if len(calls) == 1:
            # First turn: leave the gaps open so the service asks about them.
            return {"assistant_message": "两个问题。", "brief_operations": []}
        return {
            "assistant_message": "好的。",
            "brief_operations": [],
            "skipped_question_codes": ["LOGLINE"],
            "skipped_questions": [{"code": "LOGLINE", "evidence": "客户说不用管这个"}],
        }

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, "帮我做一个短剧")
        session_id = started["session_id"]
        asked = {question["code"] for question in started["questions"]}
        assert "LOGLINE" in asked, started["questions"]
        reply = _turn(client, session_id, "随便你")
        view = _state(client, session_id)

    assert "SKIP_UNVERIFIED" in reply["reason_codes"]
    assert view["brief"]["question_states"]["LOGLINE"]["status"] != "SKIPPED_BY_USER"


def test_a_quoted_skip_of_an_asked_question_is_honoured(container, project):
    calls: list[int] = []

    def handler(latest: str, state: dict) -> dict:
        calls.append(1)
        if len(calls) == 1:
            return {"assistant_message": "两个问题。", "brief_operations": []}
        return {
            "assistant_message": "好的，不问了。",
            "brief_operations": [],
            "skipped_question_codes": ["LOGLINE"],
            "skipped_questions": [{"code": "LOGLINE", "evidence": "这个先不定"}],
        }

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, "帮我做一个短剧")
        session_id = started["session_id"]
        assert "LOGLINE" in {question["code"] for question in started["questions"]}
        reply = _turn(client, session_id, "这个先不定，其他你决定")
        view = _state(client, session_id)

    assert "SKIP_UNVERIFIED" not in reply["reason_codes"]
    assert view["brief"]["question_states"]["LOGLINE"]["status"] == "SKIPPED_BY_USER"


def test_the_brief_editor_is_a_user_edit_and_never_needs_a_quote(container, project):
    """Rule 7: a direct edit is the user's own act, not a claim about text."""

    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        edited = client.post(
            f"/v1/creative/sessions/{session_id}/brief/edit",
            json={
                "operations": [
                    {
                        "op": "REPLACE",
                        "path": "setting.location",
                        "value": "subway platform",
                        "evidence": "brief editor",
                    }
                ]
            },
        )
        assert edited.status_code == 200, edited.text
        view = _state(client, session_id)

    assert view["brief"]["fields"]["setting"]["location"] == "subway platform"
    record = view["brief"]["provenance"]["setting.location"]
    assert record["source"] == "USER_EDIT"
    assert "evidence_verification" not in record


def test_the_client_chosen_format_hint_is_not_demoted_by_the_quote_check(container, project):
    """A format picked in the request body is an act, not a quote in prose."""

    container.creative_director.model_roles = ScriptedDirector(
        lambda latest, state: {"assistant_message": "ok", "brief_operations": []}
    )
    with _client(container) as client:
        started = _start(client, project.id, "帮我做一个片子", format="ADVERTISEMENT")
        session_id = started["session_id"]
        view = _state(client, session_id)

    assert view["brief"]["fields"]["format"] == "ADVERTISEMENT"
    assert view["brief"]["provenance"]["format"]["source"] == "USER_EDIT"


def test_the_deterministic_extractor_still_quotes_the_message_it_read(container, project):
    """The rules engine reads the literal message, so its USER_STATED survives.

    Two of its evidence strings used to be composite labels ("竖屏/vertical")
    that no user ever typed; they now carry the matched literal, so the same
    check that catches a forged quote passes them.
    """

    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        view = _state(client, started["session_id"])
    provenance = view["brief"]["provenance"]
    assert provenance["duration_seconds"]["source"] == "USER_STATED"
    assert provenance["aspect_ratio"]["source"] == "USER_STATED"
