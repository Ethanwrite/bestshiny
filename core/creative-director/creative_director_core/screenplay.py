"""The screenplay the director writes, and what is derived from it.

After the brief is approved the DIRECTOR model authors a structured
``Screenplay`` (schemas.py): treatment and hook, invariants and variables,
characters and relationships, scenes, beats with dialogue and one-action shot
intents, start/end states, continuity obligations, product claims and the
copy that must survive. This module validates that structure against the
narrative compiler's contract, renders it to the compiler's own line
vocabulary, derives the beat plan and the key-visual anchors from it, and
holds the *explicit* deterministic degradation - a scaffold that is labelled
as such and never presented as the director's writing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from production_domain.models import CreativeFormat
from pydantic import ValidationError

from .beats import BeatPlanner, action_line, dialogue_line, is_cjk
from .brief import get_path
from .schemas import (
    ANCHOR_PROMPT_VERSION,
    COMMERCE_FORMATS,
    Screenplay,
    normalize_name,
)

_LATIN_CLEAN = re.compile(r"[^A-Za-z0-9\-]+")
_PUNCTUATION = re.compile(r"[。；;，,\.:：!！?？\n\r\t]+")
_SPLITTERS = re.compile(
    r"\s*(?:然后|随后|接着|继而)\s*|\s+(?:and\s+then|then|afterwards|while|as)\s+|\s*(?:同时|与此同时)\s*",
    re.IGNORECASE,
)


class ScreenplayInvalid(ValueError):
    """The screenplay does not satisfy the contract; carries structured details."""

    def __init__(self, message: str, details: list[str]):
        super().__init__(message)
        self.details = details


def script_name(name: str) -> str:
    """The token the narrative compiler will parse as this character's name.

    CJK names are used as written. Latin names become one capitalized token
    (spaces to hyphens), because the compiler's actor regex reads exactly one
    leading token. The brief keeps the user's wording; this is the script's.
    """

    cleaned = " ".join(str(name).split())
    if is_cjk(cleaned):
        return re.sub(r"[^\w一-鿿·]", "", cleaned)[:16]
    token = _LATIN_CLEAN.sub("-", cleaned).strip("-")
    token = re.sub(r"-{2,}", "-", token)
    if not token:
        return "Lead"
    return token[:1].upper() + token[1:31]


def _clean_phrase(value: str) -> str:
    text = _PUNCTUATION.sub(" ", str(value or ""))
    text = _SPLITTERS.sub(" ", text)
    return " ".join(text.split())[:60]


def validate_screenplay(payload: Any) -> Screenplay:
    """Strict validation of a model or user screenplay; never a 500."""

    if not isinstance(payload, dict):
        raise ScreenplayInvalid("screenplay must be a JSON object", ["root is not an object"])
    try:
        screenplay = Screenplay.model_validate(payload)
    except ValidationError as exc:
        details = [
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg')}"
            for error in exc.errors()
        ][:20]
        raise ScreenplayInvalid("screenplay failed validation", details) from exc
    problems: list[str] = []
    for beat in screenplay.beats:
        seen: set[int] = set()
        for shot in beat.shots:
            if shot.sequence in seen:
                problems.append(f"beat {beat.sequence}: duplicate shot sequence {shot.sequence}")
            seen.add(shot.sequence)
            if shot.action is not None and not _clean_phrase(shot.action.actor):
                problems.append(f"beat {beat.sequence} shot {shot.sequence}: empty actor")
    if problems:
        raise ScreenplayInvalid("screenplay failed the shot contract", problems[:20])
    return screenplay


def screenplay_hash(content: dict[str, Any]) -> str:
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- derivation
def _scene_time(time_value: str) -> str:
    return "NIGHT" if time_value.upper() in {"NIGHT", "DUSK"} else "DAY"


def beats_from_screenplay(screenplay: Screenplay) -> list[dict[str, Any]]:
    """Materialize the beat plan (one rendered action line per shot) from a screenplay.

    The rendered ``action`` of every shot is the exact line the narrative
    compiler will read, in its own vocabulary, so compiled shots zip
    one-to-one with these intents.
    """

    scenes = {scene.key: scene for scene in screenplay.scenes}
    names = {normalize_name(character.name): character.name for character in screenplay.characters}
    beats: list[dict[str, Any]] = []
    for beat in screenplay.beats:
        scene = scenes[beat.scene_key]
        location = _clean_phrase(scene.location) or "studio"
        shots: list[dict[str, Any]] = []
        for shot in beat.shots:
            if shot.dialogue is not None:
                speaker = names.get(normalize_name(shot.dialogue.speaker), shot.dialogue.speaker)
                line = dialogue_line(script_name(speaker), shot.dialogue.text)
                shots.append(
                    {
                        "action": line,
                        "dialogue": shot.dialogue.text,
                        "speaker": speaker,
                        "shot_type": "DIALOGUE",
                        "duration": float(shot.duration),
                        "anchors": list(shot.anchors),
                        "start_state": shot.start_state,
                        "end_state": shot.end_state,
                        "gaze_target": shot.gaze_target,
                        "continuity_obligations": list(shot.continuity_obligations),
                        "description": "",
                    }
                )
                continue
            action = shot.action
            assert action is not None
            actor = names.get(normalize_name(action.actor), action.actor)
            rendered = action_line(
                script_name(actor),
                action.verb,
                prop=_clean_phrase(action.object),
                target=_clean_phrase(action.target),
                place=location,
            )
            shots.append(
                {
                    "action": rendered,
                    "dialogue": None,
                    "actor": actor,
                    "verb": action.verb,
                    "object": _clean_phrase(action.object),
                    "target": _clean_phrase(action.target),
                    "shot_type": shot.shot_type,
                    "duration": float(shot.duration),
                    "anchors": list(shot.anchors),
                    "start_state": shot.start_state,
                    "end_state": shot.end_state,
                    "gaze_target": shot.gaze_target,
                    "continuity_obligations": list(shot.continuity_obligations),
                    "description": action.description,
                }
            )
        beats.append(
            {
                "sequence": beat.sequence,
                "intent": beat.intent,
                "summary": beat.summary,
                "emotional_beat": beat.emotional_beat,
                "location": location,
                "time": _scene_time(scene.time),
                "scene_key": beat.scene_key,
                "characters": [names.get(normalize_name(name), name) for name in beat.characters],
                "shots": shots,
            }
        )
    return beats


def apply_beat_edits(screenplay: Screenplay, edited_beats: list[dict[str, Any]]) -> tuple[Screenplay, bool]:
    """Fold the user's beat/shot edits back into the screenplay structure.

    Editable: beat summary, emotional beat; per shot dialogue text, action
    object/target/description, shot type, duration, start/end state, gaze
    target. Returns the new screenplay and whether anything changed.
    """

    content = screenplay.model_dump(by_alias=True)
    changed = False
    by_sequence = {int(beat.get("sequence", 0)): beat for beat in edited_beats if isinstance(beat, dict)}
    for beat in content["beats"]:
        edited = by_sequence.get(int(beat["sequence"]))
        if edited is None:
            continue
        for key in ("summary", "emotional_beat"):
            if isinstance(edited.get(key), str) and edited[key] != beat.get(key):
                beat[key] = edited[key]
                changed = True
        edited_shots = edited.get("shots")
        if not isinstance(edited_shots, list):
            continue
        for shot, edited_shot in zip(beat["shots"], edited_shots, strict=False):
            if not isinstance(edited_shot, dict):
                continue
            for key in ("shot_type", "start_state", "end_state", "gaze_target"):
                value = edited_shot.get(key)
                if isinstance(value, str) and value and value != shot.get(key):
                    if key == "shot_type" and shot.get("dialogue"):
                        continue
                    shot[key] = value
                    changed = True
            duration = edited_shot.get("duration")
            if isinstance(duration, (int, float)) and float(duration) != float(shot.get("duration", 0)):
                shot["duration"] = float(duration)
                changed = True
            if shot.get("dialogue") and isinstance(edited_shot.get("dialogue"), str):
                text = " ".join(edited_shot["dialogue"].split())
                if text and text != shot["dialogue"]["text"]:
                    shot["dialogue"]["text"] = text
                    changed = True
            if shot.get("action"):
                for key in ("object", "target", "description"):
                    value = edited_shot.get(key)
                    if isinstance(value, str) and value != shot["action"].get(key, ""):
                        shot["action"][key] = value
                        changed = True
                verb = edited_shot.get("verb")
                if isinstance(verb, str) and verb and verb != shot["action"].get("verb"):
                    shot["action"]["verb"] = verb
                    changed = True
    if not changed:
        return screenplay, False
    return validate_screenplay(content), True


@dataclass(frozen=True)
class AnchorSpec:
    anchor_key: str
    kind: str
    title: str
    required: bool
    prompt: dict[str, Any]
    character_name: str | None = None

    @property
    def prompt_hash(self) -> str:
        return screenplay_hash({"version": ANCHOR_PROMPT_VERSION, **self.prompt})


def derive_anchor_specs(fields: dict[str, Any], screenplay: Screenplay) -> list[AnchorSpec]:
    """Anchors implied by the approved brief *and* screenplay together.

    Characters and the style key are required; scenes and props are optional;
    the product is required for commerce formats. Content hashes let the
    service version an anchor whose depiction changed.
    """

    style = {
        "medium": get_path(fields, "visual_style.medium") or "cinematic live-action",
        "palette": get_path(fields, "visual_style.palette") or "",
        "tone": fields.get("tone") or [],
        "direction": screenplay.treatment.visual_direction,
    }
    specs: list[AnchorSpec] = []
    brief_looks = {
        normalize_name(str(member.get("name", ""))): str(member.get("look") or "")
        for member in fields.get("characters") or []
        if isinstance(member, dict)
    }
    for character in screenplay.characters[:6]:
        key = normalize_name(character.name)
        specs.append(
            AnchorSpec(
                anchor_key=f"character:{key}",
                kind="CHARACTER",
                title=character.name,
                required=True,
                prompt={
                    "subject": character.name,
                    "look": character.look or brief_looks.get(key, ""),
                    "role": character.role,
                    "style": style,
                },
                character_name=character.name,
            )
        )
    format_value = str(fields.get("format") or CreativeFormat.UNSPECIFIED.value)
    product = get_path(fields, "product.name")
    if product:
        specs.append(
            AnchorSpec(
                anchor_key=f"product:{normalize_name(str(product))}",
                kind="PRODUCT",
                title=str(product),
                required=format_value in COMMERCE_FORMATS,
                prompt={
                    "subject": str(product),
                    "selling_points": get_path(fields, "product.selling_points") or [],
                    "claims": [claim.claim for claim in screenplay.product_claims if claim.must_preserve],
                    "style": style,
                },
            )
        )
    for scene in screenplay.scenes[:4]:
        specs.append(
            AnchorSpec(
                anchor_key=f"scene:{normalize_name(scene.location)}",
                kind="SCENE",
                title=scene.location,
                required=False,
                prompt={
                    "subject": scene.location,
                    "time": scene.time,
                    "interior": scene.interior,
                    "description": scene.description,
                    "style": style,
                },
            )
        )
    props: list[str] = []
    product_key = normalize_name(str(product)) if product else ""
    for beat in screenplay.beats:
        for shot in beat.shots:
            if shot.action is None or not shot.action.object:
                continue
            candidate = _clean_phrase(shot.action.object)
            key = normalize_name(candidate)
            if key and key != product_key and key not in {normalize_name(p) for p in props}:
                props.append(candidate)
    for prop in props[:3]:
        specs.append(
            AnchorSpec(
                anchor_key=f"prop:{normalize_name(prop)}",
                kind="PROP",
                title=prop,
                required=False,
                prompt={"subject": prop, "style": style},
            )
        )
    specs.append(
        AnchorSpec(
            anchor_key="style:master",
            kind="STYLE",
            title="Style key plate",
            required=True,
            prompt={
                "subject": (screenplay.treatment.visual_direction or str(fields.get("logline") or ""))[:200],
                "style": style,
            },
        )
    )
    # Deduplicate keys (two scenes may share a location).
    seen: set[str] = set()
    unique: list[AnchorSpec] = []
    for spec in specs:
        if spec.anchor_key not in seen:
            seen.add(spec.anchor_key)
            unique.append(spec)
    return unique


def anchor_keys_for_shot(shot: dict[str, Any], beat: dict[str, Any], product: str | None) -> list[str]:
    keys: list[str] = []
    actor = shot.get("speaker") or shot.get("actor")
    if actor:
        keys.append(f"character:{normalize_name(str(actor))}")
    if beat.get("location"):
        keys.append(f"scene:{normalize_name(str(beat['location']))}")
    if shot.get("object"):
        object_key = normalize_name(str(shot["object"]))
        if product and object_key == normalize_name(product):
            keys.append(f"product:{object_key}")
        else:
            keys.append(f"prop:{object_key}")
    keys.append("style:master")
    return keys


# ------------------------------------------------- explicit deterministic path
def deterministic_screenplay(fields: dict[str, Any], *, format_value: str, reason: str) -> Screenplay:
    """The labelled degradation: a scaffold, never the director's writing.

    Built from ``BeatPlanner``'s fixed per-format structure so the compile
    contract still holds. Every placeholder line is called one in
    ``unresolved`` and the treatment says the model was unavailable; the
    service records reasoner=DETERMINISTIC and the user must confirm they
    want to proceed with it.
    """

    planned = BeatPlanner().plan(fields, format_value=format_value)
    name = BeatPlanner._protagonist(fields)
    location = BeatPlanner._location(fields)
    time_of_day = BeatPlanner._time(fields)
    characters_json = [
        {
            "name": str(member.get("name") or "").strip() or name,
            "role": str(member.get("role") or ""),
            "look": str(member.get("look") or ""),
        }
        for member in (fields.get("characters") or [])
        if isinstance(member, dict) and str(member.get("name") or "").strip()
    ] or [{"name": name, "role": "protagonist", "look": ""}]
    known = {normalize_name(member["name"]) for member in characters_json}
    if normalize_name(name) not in known:
        characters_json.append({"name": name, "role": "presenter", "look": ""})
    product = str((fields.get("product") or {}).get("name") or "").strip()
    beats_json: list[dict[str, Any]] = []
    for beat in planned:
        shots_json: list[dict[str, Any]] = []
        for index, shot in enumerate(beat.shots, 1):
            if shot.dialogue:
                shots_json.append(
                    {
                        "sequence": index,
                        "shot_type": "DIALOGUE",
                        "duration": shot.duration,
                        "dialogue": {"speaker": name, "text": shot.dialogue},
                        "start_state": "",
                        "end_state": "",
                    }
                )
            else:
                verb = _verb_from_rendered(shot.action)
                shots_json.append(
                    {
                        "sequence": index,
                        "shot_type": shot.shot_type,
                        "duration": shot.duration,
                        "action": {
                            "actor": name,
                            "verb": verb,
                            "object": product if verb in {"pick_up", "raise", "place", "open"} else "",
                            "target": "",
                            "description": "placeholder staging; the director model was unavailable",
                        },
                        "start_state": "",
                        "end_state": "",
                    }
                )
        beats_json.append(
            {
                "sequence": beat.sequence,
                "intent": beat.intent,
                "summary": beat.summary,
                "scene_key": "main",
                "characters": [name],
                "shots": shots_json,
            }
        )
    content = {
        "treatment": {
            "title": str(fields.get("logline") or "Untitled")[:120],
            "premise": str(fields.get("logline") or "No logline was approved."),
            "hook": {
                "opening_question": str(fields.get("hook") or ""),
                "promise": "",
                "audience_feeling": "",
            },
            "visual_direction": str(get_path(fields, "visual_style.medium") or ""),
            "ending": "",
        },
        "invariants": [],
        "variables": [],
        "characters": characters_json,
        "scenes": [{"key": "main", "location": location, "time": time_of_day, "description": ""}],
        "beats": beats_json,
        "product_claims": [],
        "required_copy": [],
        "obligations": [],
        "unresolved": [
            f"DETERMINISTIC SCAFFOLD: the director model was unavailable ({reason}). "
            "Every dialogue line is a placeholder, not the director's writing. "
            "Redraft with the director before approving, or approve knowing this."
        ],
    }
    return validate_screenplay(content)


def _verb_from_rendered(line: str) -> str:
    lowered = line.casefold()
    for terms, verb in (
        (("举起", "raises"), "raise"),
        (("拿起", "picks up"), "pick_up"),
        (("放下", "puts down"), "place"),
        (("打开", "opens"), "open"),
        (("转身", "turns"), "turn"),
        (("看向", "looks"), "look"),
        (("走向", "walks"), "walk"),
        (("进入", "enters"), "enter"),
        (("坐下", "sits"), "sit"),
        (("站起", "stands"), "stand"),
        (("停下", "stops"), "stop"),
    ):
        if any(term in lowered for term in terms):
            return verb
    return "look"
