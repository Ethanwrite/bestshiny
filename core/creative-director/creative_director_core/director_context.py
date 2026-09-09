"""What the DIRECTOR and SHOT_PLANNER models are given, and how it is audited.

Every call carries one Skill as its system prompt - injected by the Skill
runtime, never assembled here - plus the application protocol (the JSON
contract) this module defines: the dialogue turn, the story the Director
writes (intent, facts, beats and lines; no shots), and the shot plan the Shot
Planner decomposes it into (one dominant action per shot; no framing, lens,
movement or light). The Director's context is the ordered conversation, its
earlier questions, the structured brief with per-field provenance and
question states, the user-established facts nobody may silently move, the
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
from .schemas import (
    ACTION_VERBS,
    DIALOGUE_LEAD_SECONDS,
    MAX_CAST,
    MAX_IDENTITY_CRITICAL_CHARACTERS,
    MAX_QUESTIONS_PER_TURN,
    MAX_SHOT_DURATION_SECONDS,
    MICRO_ACTIONS,
    MIN_SHOT_DURATION_SECONDS,
    SPECS_BY_CODE,
    SPEECH_CJK_CHARACTERS_PER_SECOND,
    SPEECH_WORDS_PER_SECOND,
    usable_dialogue_window,
)

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

STORY_PROTOCOL = f"""
## Writing the story (application protocol)

The brief below is approved and binding. Write an original treatment and screenplay story for it, in
the client's language for dialogue and prose. Lock the client's facts verbatim as invariants.

You decide WHY: the intent, the hook, the audience promise, the locked invariants, the editable
variables, the emotional and creative visual direction, the forbidden zones and the ending. You do
NOT decide shots: shot count, start and end states, gaze, framing, composition, lens, camera
movement, lighting, model or provider belong to later stages and any such field you write is
discarded and recorded as a breach of your authority.

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
             "dialogue": [{{"speaker": str, "text": str}}]}}],   // the required lines, in order
  "product_claims": [{{"claim": str, "must_preserve": bool}}],
  "required_copy": [{{"text": str, "beat": int}}],  // which beat the words appear in
  "obligations": [{{"key": str, "promise": str, "category": str}}],
  "unresolved": [str]
}}

Story contract:
- Every beat says what the audience now understands that they did not a moment ago, in "summary",
  and carries its required dialogue in "dialogue", in the order it is spoken. Each line is one
  short utterance a single shot can carry: about {SPEECH_CJK_CHARACTERS_PER_SECOND:g} Chinese
  characters or {SPEECH_WORDS_PER_SECOND:g} English words per second, and no shot is longer than
  {MAX_SHOT_DURATION_SECONDS:g} seconds - split a long speech into several lines.
- Every speaker and every beat character must be a character in "characters"; beat.scene_key must
  be a scene key; beats are numbered 1..n consecutively.
- Write at most {MAX_CAST} characters. Every character who appears in a beat gets a generated key
  visual and a locked identity, so one extra name is one more unanchored face; a cast over the
  limit is rejected, not trimmed. Name in "characters" only who is actually on screen - describe
  anyone who is merely referred to inside the prose instead.
- Nobody looks into the lens unless the client asked; say so in "invariants" if the client did.
- Product claims stay exact, word for word: they are recorded as narrative facts and cannot be
  reworded later by you, by the shot planner, by the prompt compiler, or by an edit.
- Every "required_copy" entry names the beat the words appear in; the shot planner places them in
  a shot. Copy with no beat blocks approval.
- Scope an invariant with "characters" and/or "scenes" when it is about them; leave both out only
  when it holds for the whole piece. A scoped invariant constrains only the shots it applies to.
- A change to identity, relationships, canonical assets, product facts, required dialogue, scene
  geography, the ending, prohibitions or commercial claims is a new approved version that
  supersedes the previous one; never present it as a silent edit.
- Mark every open creative choice in "unresolved". Never invent an answer to an open question.
- Write real, specific dialogue for this story. No placeholders.
""".strip()

SHOT_PLAN_PROTOCOL = f"""
## Decomposing the story into shots (application protocol)

The story below is the Director's and is binding: its characters, scenes, invariants, product
claims, required copy, ending and every line of dialogue are locked. You decide WHAT HAPPENS in
each generation shot: the one dominant visible action, the subject, which line is spoken, the
start state, the end state, the gaze target, the spatial state and the continuity handoff. You do
NOT decide how it is seen - shot size, framing, composition, lens, camera movement, lighting,
model or provider - and any such field you write is discarded and recorded.

Answer with ONE JSON object and nothing else, exactly this shape:
{{
  "beats": [{{"sequence": int,                 // one entry per story beat, same numbering
             "shots": [{{"sequence": int, "duration": number,
                        "action": {{"actor": str, "verb": str, "object": str, "target": str,
                                   "description": str}} | null,
                        "dialogue": {{"speaker": str, "text": str}} | null,
                        "micro_actions": [str],
                        "present_characters": [str], "identity_critical_characters": [str],
                        "start_state": str, "end_state": str, "gaze_target": str,
                        "continuity_obligations": [str]}}]}}],
  "required_copy": [{{"text": str, "beat": int, "shot": int}}],  // where the story's copy lands
  "mobile_hook_check": str,   // what the first seconds make a phone viewer notice, ask or feel
  "unresolved": [str]
}}

Shot contract (one generation shot = one dominant visual action):
- Every shot carries ONE dominant visual action ("action", one verb) and may carry 0..1 short
  "dialogue" line at the same time. A shot with a line and no action is a speaking shot: delivering
  the line is its visible action. Never both null.
- action.verb must be one of: {", ".join(ACTION_VERBS)}. Describe the staging in action.description.
  Micro-actions ({", ".join(MICRO_ACTIONS)}) may ride along in "micro_actions" and do not count as
  a second action. Never stage two consecutive narrative actions in one shot ("she opens the door,
  then walks in" is two shots) - a description that sequences actions is rejected.
- Place every line of every beat exactly once, verbatim, in the story's order, each in its own
  shot or beside one action. Never write, drop, merge or reword a line: a plan whose lines differ
  from the story is rejected whole.
- A line must fit its shot: about {SPEECH_CJK_CHARACTERS_PER_SECOND:g} Chinese characters or
  {SPEECH_WORDS_PER_SECOND:g} English words per second, with {DIALOGUE_LEAD_SECONDS:g}s of air at
  each end of the shot. A 3-second shot carries roughly
  {int(usable_dialogue_window(3) * SPEECH_CJK_CHARACTERS_PER_SECOND)} characters or
  {int(usable_dialogue_window(3) * SPEECH_WORDS_PER_SECOND)} words; a line that cannot be said in
  its shot is rejected.
- Duration is the planner's intent, {MIN_SHOT_DURATION_SECONDS:g}-{MAX_SHOT_DURATION_SECONDS:g}
  seconds per shot; the total should approximate the brief's duration. Which model renders a shot,
  and at what execution length, is decided later from the model's capability - never assume one.
  Shot size is not yours either: do not write shot_type, framing or a lens.
- "present_characters" lists everyone visible in the frame (staged in the prompt).
  "identity_critical_characters" is the subset whose face the audience must recognise (at most
  {MAX_IDENTITY_CRITICAL_CHARACTERS}); only they are sent to the model as identity references, so a
  background figure is present, not identity-critical. Both default to the actor and the speaker.
- Every actor, speaker and present character must be a character of the story; never add one.
- State an explicit start_state, end_state and gaze_target for every shot, and chain them: each
  end state is the next shot's start state. Name every gaze target; nobody looks into the lens
  unless the story's invariants say the client asked.
- Every "required_copy" entry of the story must name the beat and shot the words appear in.
- Return an unresolved story or hook question in "unresolved" instead of answering it here.
""".strip()

#: The whole authoring contract, story then shots, for readers and tests.
SCREENPLAY_PROTOCOL = f"{STORY_PROTOCOL}\n\n{SHOT_PLAN_PROTOCOL}"


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
    #: The Skill runtime's record of the call that consumed this context
    #: (resolved / loaded / model_invoked / execution_mode / fallback_reason).
    skill_invocation: dict[str, Any] | None = None

    def as_json(self) -> dict[str, Any]:
        payload = {
            "turns_total": self.turns_total,
            "turns_verbatim": self.turns_verbatim,
            "turns_condensed": self.turns_condensed,
            "compressed": self.compressed,
            "preserved": dict(self.preserved),
            "context_hash": self.context_hash,
            "skill_version": self.skill_version,
            "skill_content_hash": self.skill_content_hash,
        }
        if self.skill_invocation is not None:
            payload["skill_invocation"] = dict(self.skill_invocation)
        return payload

    def record(self, invocation: Any) -> None:
        """Stamp the runtime's invocation onto this audit: the loaded Skill, the call hash."""

        self.skill_invocation = invocation.as_json()
        self.context_hash = invocation.context_hash or self.context_hash
        self.skill_version = invocation.version if invocation.loaded else None
        self.skill_content_hash = invocation.content_hash if invocation.loaded else None


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
    turns: list[dict[str, Any]],
    fields: dict[str, Any],
    provenance: dict[str, Any],
    question_states: dict[str, Any],
    stage: str,
    format_value: str,
    latest_user_message: str,
    approved: dict[str, Any],
    analysis_questions: list[dict[str, Any]],
    skill: Any = None,
) -> tuple[list[dict[str, Any]], ContextAudit]:
    """The conversation and state for one dialogue turn, plus its audit.

    The system message - the Director Skill body and ``TURN_PROTOCOL`` - is
    added by the Skill runtime, which is the only place a Skill body enters a
    model call. ``skill`` is accepted for source compatibility and ignored.
    """

    del skill
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
        *history,
        {"role": "user", "content": json.dumps(state_block, ensure_ascii=False, default=str)},
    ]
    audit.preserved = counts
    audit.context_hash = _hash({"messages": messages})
    return messages, audit


def build_story_messages(
    *,
    turns: list[dict[str, Any]],
    fields: dict[str, Any],
    provenance: dict[str, Any],
    format_value: str,
    previous_story: dict[str, Any] | None,
    user_notes: str,
) -> tuple[list[dict[str, Any]], ContextAudit]:
    """The conversation and request for the Director's story call, plus its audit."""

    history, audit = compress_history(turns)
    preserved, counts = preserved_block(turns, fields, provenance, {}, {"brief": "APPROVED"})
    request = {
        "task": "REVISE_STORY" if previous_story else "WRITE_STORY",
        "approved_brief": fields,
        "format": format_value,
        "client_established_facts": preserved["client_established_facts"],
        "prohibitions": preserved["prohibitions"],
        "previous_story": previous_story,
        "client_revision_notes": user_notes,
    }
    messages: list[dict[str, Any]] = [
        *history,
        {"role": "user", "content": json.dumps(request, ensure_ascii=False, default=str)},
    ]
    audit.preserved = counts
    audit.context_hash = _hash({"messages": messages})
    return messages, audit


def build_shot_plan_messages(
    *,
    story: dict[str, Any],
    fields: dict[str, Any],
    format_value: str,
    prohibitions: list[str],
    previous_plan: dict[str, Any] | None,
    user_notes: str,
) -> tuple[list[dict[str, Any]], ContextAudit]:
    """The request for the Shot Planner's call: the locked story, nothing else creative.

    No conversation history travels here - the story is the whole brief the
    planner may read, so it cannot be argued into rewriting a line from
    something the client said three turns ago.
    """

    request = {
        "task": "REVISE_SHOTS" if previous_plan else "PLAN_SHOTS",
        "format": format_value,
        "duration_seconds": fields.get("duration_seconds"),
        "aspect_ratio": fields.get("aspect_ratio"),
        "platform": fields.get("platform"),
        "story": story,
        "prohibitions": prohibitions,
        "previous_shot_plan": previous_plan,
        "client_revision_notes": user_notes,
    }
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": json.dumps(request, ensure_ascii=False, default=str)},
    ]
    audit = ContextAudit(0, 0, 0, False)
    audit.context_hash = _hash({"messages": messages})
    return messages, audit


#: Source compatibility: the story call is what the screenplay call became.
build_screenplay_messages = build_story_messages
