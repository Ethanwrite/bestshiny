"""What the DIRECTOR model is given each turn, and how it is audited.

Every call carries the Director Skill as its system prompt plus the
application protocol (the JSON contract), the ordered conversation, the
director's earlier questions, the structured brief with per-field provenance
and question states, the user-established facts nobody may silently move, the
workflow stage and the user's latest message. A long conversation is
compressed on record - the audit says what was condensed - but user facts,
corrections, prohibitions, open questions and approved content are always
carried verbatim. Provider names, model choice, quotas and retry tactics
never appear here: they are not creative context.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .brief import BriefEngine
from .schemas import ACTION_VERBS, MAX_CAST, MAX_QUESTIONS_PER_TURN, SHOT_TYPES, SPECS_BY_CODE

#: Conversation budget, in characters of turn content, before compression.
HISTORY_CHAR_BUDGET = 14000
#: Turns that always stay verbatim at the end of a compressed conversation.
VERBATIM_TAIL_TURNS = 10
#: How much of a condensed turn survives.
CONDENSED_TURN_CHARS = 160
#: Per-turn content cap in the verbatim window.
TURN_CONTENT_CAP = 4000

TURN_PROTOCOL = """
## Working in this conversation (application protocol)

You are in a multi-turn conversation with the client. You see the ordered conversation, your own
earlier questions, the structured brief with the provenance of every field, the state of every
question, and the facts the client established (never change those on your own reading).

Answer with ONE JSON object and nothing else:
{
  "assistant_message": string,          // your words to the client, in the client's language
  "brief_operations": [                 // explicit changes to the brief
    {"op": "SET"|"REPLACE"|"UPSERT"|"REMOVE"|"KEEP", "path": string, "value": any,
     "evidence": string, "evidence_turn_id": string|null,
     "confidence": "USER_STATED"|"INFERRED"}
  ],
  "answered_question_codes": [string],  // codes the client just answered
  "skipped_question_codes": [string],   // codes the client explicitly declined to answer
  "skipped_questions": [{"code": string, "evidence": string, "evidence_turn_id": string|null}],
  "unresolved_questions": [{"code": string, "question": string}],  // at most three, highest value first
  "assumptions": [{"path": string, "value": any, "rationale": string}],  // what you would assume
  "creative_notes": [string]            // directions worth remembering
}

Rules:
- Paths: format, logline, duration_seconds, platform, aspect_ratio, hook, call_to_action, audience,
  tone (list), visual_style.medium, visual_style.palette, setting.location, setting.time,
  product.name, product.selling_points (list), music.mood, characters (list of
  {name, role, look, wants, relationships:[{with, relation}]}).
- SET fills an empty field. REPLACE changes a field the client already established and is honoured
  only with confidence USER_STATED and the client's words as evidence. UPSERT adds or updates one
  character (matched by name) or list member. REMOVE deletes on the client's explicit request.
  Never rename, replace or remove a client fact on an inference. Quote the client in "evidence".
- "evidence" is checked against the client's own messages, verbatim (case, spacing and punctuation
  are ignored; wording is not). An operation whose evidence cannot be found in something the client
  actually wrote is recorded as INFERRED however it is labelled - so quote, do not paraphrase, and
  say INFERRED when you are reading between the lines. "evidence_turn_id" may name the client turn
  the quote comes from; naming one that does not exist fails the check.
- A skip is honoured only for a question that was actually asked and whose refusal the client's own
  words support: list it in "skipped_questions" with the quote. A bare code in
  "skipped_question_codes" is recorded but leaves the question open.
- Ask at most three questions, only about fields that are missing and high-value. Never repeat a
  question the client already answered. You may re-confirm an unanswered one in context.
- Never invent answers. Put your reading of open points in "assumptions", never in SET with
  USER_STATED. Assumptions are shown to the client for confirmation.
- Write in the client's language. Be concrete and warm; offer a creative direction when it helps.
  When the brief is complete, say so and invite approval.
""".strip()

SCREENPLAY_PROTOCOL = f"""
## Writing the screenplay (application protocol)

The brief below is approved and binding. Write an original treatment and screenplay for it, in
the client's language for dialogue and prose. Lock the client's facts verbatim as invariants.

Answer with ONE JSON object and nothing else, exactly this shape:
{{
  "treatment": {{"title": str, "premise": str,
                "hook": {{"opening_question": str, "promise": str, "audience_feeling": str}},
                "audience_expectation": str, "tone_direction": str, "visual_direction": str,
                "ending": str}},
  "invariants": [{{"text": str, "characters": [str], "scenes": [str]}}],  // scope, or omit for global
  "variables": [str],
  "characters": [{{"name": str, "role": str, "look": str, "wants": str,
                  "relationships": [{{"with": str, "relation": str}}]}}],
  "scenes": [{{"key": str, "location": str, "time": "DAY"|"NIGHT"|"DUSK"|"DAWN",
              "interior": bool, "description": str}}],
  "beats": [{{"sequence": int, "intent": str, "summary": str, "scene_key": str,
             "characters": [str], "emotional_beat": str,
             "shots": [{{"sequence": int, "shot_type": str, "duration": number,
                        "action": {{"actor": str, "verb": str, "object": str, "target": str,
                                   "description": str}} | null,
                        "dialogue": {{"speaker": str, "text": str}} | null,
                        "start_state": str, "end_state": str, "gaze_target": str,
                        "continuity_obligations": [str]}}]}}],
  "product_claims": [{{"claim": str, "must_preserve": bool}}],
  "required_copy": [{{"text": str, "beat": int, "shot": int}}],  // where the words are on screen
  "obligations": [{{"key": str, "promise": str, "category": str}}],
  "unresolved": [str]
}}

Shot contract (generation shots are single-action):
- Every shot has EXACTLY ONE primary element: an "action" OR a "dialogue", never both, never none.
- action.verb must be one of: {", ".join(ACTION_VERBS)}. One visible action per shot; describe
  the staging in action.description.
- shot_type is one of: {", ".join(SHOT_TYPES)}. It is a suggestion the shot planner may
  refine (framing, lens, movement and light are not decided here); use MEDIUM when unsure.
  Duration is 2-10 seconds per shot; the total should approximate the brief's duration.
- Every actor and speaker must be a character in "characters"; beat.scene_key must be a scene key;
  beats are numbered 1..n consecutively.
- Write at most {MAX_CAST} characters. Every character who appears in a beat or a shot gets a
  generated key visual and a locked identity, so one extra name is one more unanchored face; a
  cast over the limit is rejected, not trimmed. Name in "characters" only who is actually on
  screen - describe anyone who is merely referred to inside the prose instead.
- State an explicit start_state, end_state and gaze_target for every shot. Nobody looks into the
  lens unless the client asked.
- Product claims stay exact, word for word: they are recorded as narrative facts and cannot be
  reworded later by you, by the prompt compiler, or by an edit.
- Every "required_copy" entry must name the beat and shot the words appear in. Copy with no
  placement blocks approval - say where it is on screen rather than leaving it to the platform.
- Scope an invariant with "characters" and/or "scenes" when it is about them; leave both out only
  when it holds for the whole piece. A scoped invariant constrains only the shots it applies to.
- Mark every open creative choice in "unresolved".
- Write real, specific dialogue for this story. No placeholders.
""".strip()


@dataclass
class ContextAudit:
    turns_total: int
    turns_verbatim: int
    turns_condensed: int
    compressed: bool
    preserved: dict[str, int] = field(default_factory=dict)
    context_hash: str = ""
    skill_version: str | None = None
    skill_content_hash: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "turns_total": self.turns_total,
            "turns_verbatim": self.turns_verbatim,
            "turns_condensed": self.turns_condensed,
            "compressed": self.compressed,
            "preserved": dict(self.preserved),
            "context_hash": self.context_hash,
            "skill_version": self.skill_version,
            "skill_content_hash": self.skill_content_hash,
        }


@dataclass(frozen=True)
class SkillText:
    system_prompt: str
    version: str | None
    content_hash: str | None


def _turn_text(turn: dict[str, Any]) -> str:
    content = str(turn.get("content") or "").strip()
    if turn.get("speaker") == "DIRECTOR" and turn.get("questions"):
        asked = "\n".join(
            f"- {question.get('question')}"
            for question in turn["questions"]
            if isinstance(question, dict) and question.get("question")
        )
        if asked:
            content = f"{content}\n{asked}" if content else asked
    return content


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compress_history(turns: list[dict[str, Any]]) -> tuple[list[dict[str, str]], ContextAudit]:
    """Chat-shaped history within budget; the audit says what was condensed."""

    total_chars = sum(len(_turn_text(turn)) for turn in turns)
    if total_chars <= HISTORY_CHAR_BUDGET or len(turns) <= VERBATIM_TAIL_TURNS + 1:
        verbatim: list[dict[str, str]] = [
            {
                "role": "user" if turn.get("speaker") == "USER" else "assistant",
                "content": _turn_text(turn)[:TURN_CONTENT_CAP],
            }
            for turn in turns
            if _turn_text(turn)
        ]
        return verbatim, ContextAudit(len(turns), len(turns), 0, False)
    head = turns[:1]
    tail = turns[-VERBATIM_TAIL_TURNS:]
    middle = turns[1:-VERBATIM_TAIL_TURNS]
    messages: list[dict[str, str]] = []
    for turn in head:
        messages.append(
            {
                "role": "user" if turn.get("speaker") == "USER" else "assistant",
                "content": _turn_text(turn)[:TURN_CONTENT_CAP],
            }
        )
    condensed_lines = []
    for turn in middle:
        text = _turn_text(turn).replace("\n", " ")
        speaker = "CLIENT" if turn.get("speaker") == "USER" else "DIRECTOR"
        condensed_lines.append(
            f"- {speaker}: {text[:CONDENSED_TURN_CHARS]}{'…' if len(text) > CONDENSED_TURN_CHARS else ''}"
        )
    if condensed_lines:
        messages.append(
            {
                "role": "user",
                "content": "[Earlier conversation, condensed by the application; every client fact, "
                "correction and prohibition is restated in the state block below]\n"
                + "\n".join(condensed_lines),
            }
        )
    for turn in tail:
        messages.append(
            {
                "role": "user" if turn.get("speaker") == "USER" else "assistant",
                "content": _turn_text(turn)[:TURN_CONTENT_CAP],
            }
        )
    return messages, ContextAudit(len(turns), len(head) + len(tail), len(middle), True)


def preserved_block(
    turns: list[dict[str, Any]],
    fields: dict[str, Any],
    provenance: dict[str, Any],
    question_states: dict[str, Any],
    approved: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    """Facts that survive any compression, and how many of each."""

    facts = BriefEngine.user_facts(fields, provenance)
    corrections: list[dict[str, Any]] = []
    for turn in turns:
        if turn.get("speaker") != "DIRECTOR":
            continue
        for applied in turn.get("operations") or []:
            if isinstance(applied, dict) and applied.get("op") in {"REPLACE", "REMOVE"}:
                corrections.append(
                    {
                        "op": applied.get("op"),
                        "path": applied.get("path"),
                        "value": applied.get("value"),
                        "turn": turn.get("sequence"),
                    }
                )
    prohibitions: list[str] = []
    for turn in turns:
        if turn.get("speaker") == "USER":
            prohibitions.extend(BriefEngine.prohibitions(str(turn.get("content") or "")))
    unanswered = [
        {"code": code, "question": SPECS_BY_CODE[code].question, "status": state.get("status")}
        for code, state in question_states.items()
        if code in SPECS_BY_CODE and state.get("status") in {"ASKED", "SKIPPED_BY_USER"}
    ]
    block = {
        "client_established_facts": facts,
        "corrections": corrections[-20:],
        "prohibitions": prohibitions[-20:],
        "unanswered_questions": unanswered,
        "approved": approved,
    }
    counts = {
        "facts": len(facts),
        "corrections": len(corrections),
        "prohibitions": len(prohibitions),
        "unanswered": len(unanswered),
        "approved": len(approved),
    }
    return block, counts


def build_turn_messages(
    *,
    skill: SkillText,
    turns: list[dict[str, Any]],
    fields: dict[str, Any],
    provenance: dict[str, Any],
    question_states: dict[str, Any],
    stage: str,
    format_value: str,
    latest_user_message: str,
    approved: dict[str, Any],
    analysis_questions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], ContextAudit]:
    """The complete message list for one dialogue turn, plus its audit."""

    history, audit = compress_history(turns)
    preserved, counts = preserved_block(turns, fields, provenance, question_states, approved)
    state_block = {
        "stage": stage,
        "format": format_value,
        "brief": fields,
        "field_provenance": {
            key: {
                "source": record.get("source"),
                "operation": record.get("operation"),
                "turn": record.get("turn_sequence"),
            }
            for key, record in provenance.items()
            if isinstance(record, dict)
        },
        "question_states": question_states,
        "gap_candidates": analysis_questions[:MAX_QUESTIONS_PER_TURN],
        "preserved": preserved,
        "latest_client_message": latest_user_message,
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"{skill.system_prompt}\n\n{TURN_PROTOCOL}"},
        *history,
        {"role": "user", "content": json.dumps(state_block, ensure_ascii=False, default=str)},
    ]
    audit.preserved = counts
    audit.context_hash = _hash({"messages": messages})
    audit.skill_version = skill.version
    audit.skill_content_hash = skill.content_hash
    return messages, audit


def build_screenplay_messages(
    *,
    skill: SkillText,
    turns: list[dict[str, Any]],
    fields: dict[str, Any],
    provenance: dict[str, Any],
    format_value: str,
    previous_screenplay: dict[str, Any] | None,
    user_notes: str,
) -> tuple[list[dict[str, Any]], ContextAudit]:
    """The message list for the screenplay-writing call, plus its audit."""

    history, audit = compress_history(turns)
    preserved, counts = preserved_block(turns, fields, provenance, {}, {"brief": "APPROVED"})
    request = {
        "task": "REVISE_SCREENPLAY" if previous_screenplay else "WRITE_SCREENPLAY",
        "approved_brief": fields,
        "format": format_value,
        "client_established_facts": preserved["client_established_facts"],
        "prohibitions": preserved["prohibitions"],
        "previous_screenplay": previous_screenplay,
        "client_revision_notes": user_notes,
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"{skill.system_prompt}\n\n{SCREENPLAY_PROTOCOL}"},
        *history,
        {"role": "user", "content": json.dumps(request, ensure_ascii=False, default=str)},
    ]
    audit.preserved = counts
    audit.context_hash = _hash({"messages": messages})
    audit.skill_version = skill.version
    audit.skill_content_hash = skill.content_hash
    return messages, audit
