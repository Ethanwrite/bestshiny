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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from production_domain.models import CreativeFormat
from pydantic import ValidationError

from .beats import BeatPlanner, action_line, dialogue_line, is_cjk
from .brief import get_path
from .schemas import (
    ANCHOR_PROMPT_VERSION,
    COMMERCE_FORMATS,
    MAX_CAST,
    MAX_PROP_ANCHORS,
    MAX_SCENE_ANCHORS,
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


def compiler_location(location: str) -> str:
    """The location exactly as the scene heading, and so the Location row, names it."""

    return _clean_phrase(location) or "studio"


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
    # Two names the schema keeps apart can still be one token once rendered
    # for the script: "Mary Jane" and "Mary-Jane" both become `Mary-Jane`, the
    # compiler reads one actor, and the Character row (matched case-blind on
    # that token) is shared - two identities, one Canon. Refused here, where
    # the script token is minted, rather than discovered at the lock.
    by_script_name: dict[str, str] = {}
    for character in screenplay.characters:
        token = script_name(character.name).casefold()
        other = by_script_name.get(token)
        if other is not None and other != character.name:
            problems.append(
                f"characters {other!r} and {character.name!r} collapse to the same script name "
                f"{script_name(character.name)!r}; rename one of them"
            )
        by_script_name.setdefault(token, character.name)
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
        location = compiler_location(scene.location)
        shots: list[dict[str, Any]] = []
        for shot in beat.shots:
            if shot.dialogue is not None:
                speaker = names.get(normalize_name(shot.dialogue.speaker), shot.dialogue.speaker)
                line = dialogue_line(script_name(speaker), shot.dialogue.text)
                shots.append(
                    {
                        "sequence": shot.sequence,
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
                    "sequence": shot.sequence,
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
                # The anchor key is minted from the scene's *raw* location, so
                # a location carrying punctuation or over 60 characters still
                # resolves to the plate the bible locked. `location` above is
                # the compiler's own cleaned line vocabulary.
                "location_key": normalize_name(scene.location),
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
    #: For SCENE anchors, the scene keys this location covers; for PROP and
    #: PRODUCT anchors the normalized subject key. Lets the bible lock bind a
    #: canonical asset back to the screenplay element it depicts.
    subject_key: str = ""
    scene_keys: tuple[str, ...] = ()

    @property
    def prompt_hash(self) -> str:
        return screenplay_hash({"version": ANCHOR_PROMPT_VERSION, **self.prompt})


@dataclass(frozen=True)
class UncoveredElement:
    """A screenplay element that deliberately gets no key visual, and why."""

    kind: str
    title: str
    reason: str

    def as_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "title": self.title, "reason": self.reason}


@dataclass(frozen=True)
class AnchorDerivation:
    """What the screenplay implies, and what it deliberately leaves uncovered."""

    specs: tuple[AnchorSpec, ...]
    uncovered: tuple[UncoveredElement, ...]

    def coverage_json(self) -> dict[str, Any]:
        return {
            "anchored": [spec.anchor_key for spec in self.specs],
            "uncovered": [item.as_json() for item in self.uncovered],
        }


def appearing_character_keys(screenplay: Screenplay) -> set[str]:
    """Normalized names of every character that is actually on screen.

    A character acts, speaks, or is listed as present in a beat. Anyone in that
    set needs a required key visual and an identity lock; anyone outside it is
    named in the treatment only and is recorded as uncovered, never silently
    dropped.
    """

    appearing: set[str] = set()
    for beat in screenplay.beats:
        for name in beat.characters:
            appearing.add(normalize_name(name))
        for shot in beat.shots:
            if shot.dialogue is not None:
                appearing.add(normalize_name(shot.dialogue.speaker))
            if shot.action is not None:
                appearing.add(normalize_name(shot.action.actor))
    return appearing


class ScreenplayCastOverflow(ScreenplayInvalid):
    """The screenplay names more characters than the pipeline can anchor."""


def derive_anchor_specs(fields: dict[str, Any], screenplay: Screenplay) -> list[AnchorSpec]:
    """The anchors implied by the approved brief and screenplay (specs only)."""

    return list(derive_anchors(fields, screenplay).specs)


def derive_anchors(fields: dict[str, Any], screenplay: Screenplay) -> AnchorDerivation:
    """Anchors implied by the approved brief *and* screenplay together.

    Every character that appears in a beat or shot is required, as is the style
    key; the product is required for commerce formats. Scenes a beat plays in
    are anchored so the frame-anchor planner can always resolve a canonical
    location. Nothing is sliced away in silence: a character named only in the
    treatment, a scene no beat uses and a prop beyond the prop budget are all
    returned as explicit ``UncoveredElement`` records with their reason.
    """

    style = {
        "medium": get_path(fields, "visual_style.medium") or "cinematic live-action",
        "palette": get_path(fields, "visual_style.palette") or "",
        "tone": fields.get("tone") or [],
        "direction": screenplay.treatment.visual_direction,
    }
    specs: list[AnchorSpec] = []
    uncovered: list[UncoveredElement] = []
    brief_looks = {
        normalize_name(str(member.get("name", ""))): str(member.get("look") or "")
        for member in fields.get("characters") or []
        if isinstance(member, dict)
    }
    if len(screenplay.characters) > MAX_CAST:
        # Unreachable through validate_screenplay (the schema caps the list),
        # but derive_anchors is also called on hand-built structures; refuse
        # rather than slice, because a sliced character still acts on screen.
        raise ScreenplayCastOverflow(
            f"the screenplay names {len(screenplay.characters)} characters; at most {MAX_CAST} "
            "can be given a key visual and an identity lock",
            [f"characters: {len(screenplay.characters)} > {MAX_CAST}"],
        )
    appearing = appearing_character_keys(screenplay)
    for character in screenplay.characters:
        key = normalize_name(character.name)
        if key not in appearing:
            # Named in the treatment but in no beat and no shot: nothing will
            # ever render this face, so it needs no key visual - on record.
            uncovered.append(
                UncoveredElement(
                    kind="CHARACTER",
                    title=character.name,
                    reason="NOT_IN_ANY_BEAT_OR_SHOT",
                )
            )
            continue
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
                subject_key=key,
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
                subject_key=normalize_name(str(product)),
            )
        )
    played_scene_keys = {beat.scene_key for beat in screenplay.beats}
    scene_keys_by_location: dict[str, list[str]] = {}
    for scene in screenplay.scenes:
        if scene.key in played_scene_keys:
            scene_keys_by_location.setdefault(normalize_name(scene.location), []).append(scene.key)
    anchored_locations = 0
    for scene in screenplay.scenes:
        location_key = normalize_name(scene.location)
        if scene.key not in played_scene_keys:
            uncovered.append(
                UncoveredElement(
                    kind="SCENE", title=scene.location, reason="SCENE_NOT_USED_BY_ANY_BEAT"
                )
            )
            continue
        if any(spec.anchor_key == f"scene:{location_key}" for spec in specs):
            continue
        if anchored_locations >= MAX_SCENE_ANCHORS:
            uncovered.append(
                UncoveredElement(kind="SCENE", title=scene.location, reason="SCENE_ANCHOR_LIMIT")
            )
            continue
        anchored_locations += 1
        specs.append(
            AnchorSpec(
                anchor_key=f"scene:{location_key}",
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
                subject_key=location_key,
                scene_keys=tuple(scene_keys_by_location.get(location_key, (scene.key,))),
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
    for prop in props[:MAX_PROP_ANCHORS]:
        specs.append(
            AnchorSpec(
                anchor_key=f"prop:{normalize_name(prop)}",
                kind="PROP",
                title=prop,
                required=False,
                prompt={"subject": prop, "style": style},
                subject_key=normalize_name(prop),
            )
        )
    for prop in props[MAX_PROP_ANCHORS:]:
        uncovered.append(UncoveredElement(kind="PROP", title=prop, reason="PROP_ANCHOR_LIMIT"))
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
    return AnchorDerivation(tuple(unique), tuple(uncovered))


@dataclass(frozen=True)
class ShotConstraints:
    """What a single shot must honour, scoped to that shot alone."""

    beat_sequence: int
    shot_sequence: int
    invariants: tuple[str, ...] = ()
    product_claims: tuple[str, ...] = ()
    required_copy: tuple[str, ...] = ()
    #: What the user forbade, in their own sentences, and the things those
    #: sentences forbid. Global by nature - a prohibition holds in every shot -
    #: so every shot carries them into its prompt and its QC checklist.
    prohibitions: tuple[str, ...] = ()
    prohibited_terms: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "invariants": list(self.invariants),
            "product_claims": list(self.product_claims),
            "required_copy": list(self.required_copy),
            "prohibitions": list(self.prohibitions),
            "prohibited_terms": list(self.prohibited_terms),
        }

    def __bool__(self) -> bool:
        return bool(
            self.invariants or self.product_claims or self.required_copy or self.prohibitions
        )


def preserved_product_claims(
    screenplay: Screenplay, selling_points: Sequence[str] = ()
) -> list[str]:
    """Every product claim that must survive verbatim, the user's own first.

    The director may echo a selling point the user stated as a claim with
    ``must_preserve=false``, or leave it out of ``product_claims`` altogether;
    either way it then reached no shot. A selling point the brief establishes
    is preserved on the user's authority, not the director's flag: it is
    listed as written, and any claim that restates it is preserved too.
    """

    wanted = [str(item).strip() for item in selling_points if str(item).strip()]
    wanted_keys = [normalize_name(item) for item in wanted]
    result: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        key = normalize_name(text)
        if key and key not in seen:
            seen.add(key)
            result.append(text)

    for item in wanted:
        add(item)
    for claim in screenplay.product_claims:
        restates_user = any(
            key and (key in normalize_name(claim.claim) or normalize_name(claim.claim) in key)
            for key in wanted_keys
        )
        if claim.must_preserve or restates_user:
            add(claim.claim)
    return result


def normalize_anchor_key(value: str) -> str:
    """A declared anchor key in the form the derived keys use.

    The derived keys are ``kind:normalized subject``; a user writing
    ``character:Ren`` by hand means the same anchor as ``character:ren``.
    """

    text = " ".join(str(value or "").split())
    kind, separator, subject = text.partition(":")
    if not separator:
        return text
    return f"{kind.strip().casefold()}:{normalize_name(subject)}"


def merge_shot_anchors(declared: Sequence[str], derived: Sequence[str]) -> list[str]:
    """The shot's anchors: what was declared on it, then what its line implies.

    ``beats_from_screenplay`` carries the screenplay's own ``shot.anchors``
    (a second character in frame, a key visual the user bound by hand) and
    ``anchor_keys_for_shot`` derives the actor, the location, the prop and the
    style from the rendered line. The plan used to keep only the derived
    list, so the explicit bindings the user approved were gone before the
    compiler ever saw them. Declared first, so a deliberate binding is never
    demoted behind an inferred one; duplicates fold.
    """

    merged: list[str] = []
    for item in [*(normalize_anchor_key(key) for key in declared), *derived]:
        if item and item not in merged:
            merged.append(item)
    return merged


#: Characters that can carry a word: everything else is a boundary. Explicit
#: rather than \b, because \b is meaningless between two CJK characters and a
#: CJK name is matched by position, not by spacing.
_WORD_CHARACTERS = re.compile(r"[0-9A-Za-z_\u00c0-\u024f]")


def _mentions(text: str, name: str) -> bool:
    """Whether `text` names `name`, without matching it inside another word.

    A one- or two-letter cast name is ordinary in this product's audience, and
    a bare substring test made "Al" match inside "always" - which silently
    turned a global invariant into a character-scoped one and dropped it from
    the ledger and from most shots.
    """

    needle = " ".join(str(name or "").casefold().split())
    if not needle:
        return False
    haystack = str(text or "").casefold()
    start = haystack.find(needle)
    while start >= 0:
        before = haystack[start - 1] if start else ""
        after = haystack[start + len(needle) : start + len(needle) + 1]
        if not (_WORD_CHARACTERS.match(before) or _WORD_CHARACTERS.match(after)):
            return True
        start = haystack.find(needle, start + 1)
    return False


def _invariant_scope(
    invariant: Any, screenplay: Screenplay
) -> tuple[frozenset[str], frozenset[str]]:
    """Which characters and scenes an invariant is about.

    An explicit scope from the director wins. Otherwise the scope is read from
    the invariant's own words: an invariant that names a character or a
    location is about that character or that location, and one that names
    neither holds for the whole piece. This is what stops every invariant from
    being injected into every shot.
    """

    if invariant.characters or invariant.scenes:
        return (
            frozenset(normalize_name(name) for name in invariant.characters),
            frozenset(str(key) for key in invariant.scenes),
        )
    text = invariant.text
    characters = frozenset(
        normalize_name(character.name)
        for character in screenplay.characters
        if _mentions(text, character.name)
    )
    scenes = frozenset(
        scene.key for scene in screenplay.scenes if _mentions(text, scene.location)
    )
    return characters, scenes


def global_invariants(screenplay: Screenplay) -> list[str]:
    """Invariants that hold for the whole piece, by declared *or* read scope.

    These are the ones worth putting on the narrative ledger: a fact about the
    world, true in every shot. A scoped invariant is carried by the shots it
    applies to instead, so the ledger does not fill up with rules about one
    character's face.
    """

    return [
        item.text
        for item in screenplay.invariants
        if not any(_invariant_scope(item, screenplay))
    ]


def shot_constraints(  # noqa: PLR0913 - one call carries everything a shot must honour
    screenplay: Screenplay,
    *,
    product: str | None = None,
    selling_points: Sequence[str] = (),
    prohibitions: Sequence[str] = (),
    prohibited_terms: Sequence[str] = (),
) -> list[ShotConstraints]:
    """Per-shot invariants, product claims, required copy and prohibitions, in shot order.

    The order matches ``render_script``'s: one entry per shot that renders an
    action line, so the list zips with the compiled shots.
    """

    names = {normalize_name(character.name): character.name for character in screenplay.characters}
    scoped = [(item, *_invariant_scope(item, screenplay)) for item in screenplay.invariants]
    preserved = tuple(preserved_product_claims(screenplay, selling_points))
    forbidden = tuple(str(item).strip() for item in prohibitions if str(item).strip())
    forbidden_terms = tuple(str(item).strip() for item in prohibited_terms if str(item).strip())
    product_key = normalize_name(product) if product else ""
    copy_by_position: dict[tuple[int, int], list[str]] = {}
    for item in screenplay.required_copy:
        if item.placed:
            copy_by_position.setdefault((int(item.beat), int(item.shot)), []).append(item.text)
    result: list[ShotConstraints] = []
    for beat in screenplay.beats:
        beat_characters = {normalize_name(name) for name in beat.characters}
        for shot in beat.shots:
            speaker = shot.dialogue.speaker if shot.dialogue else shot.action.actor  # type: ignore[union-attr]
            # `shot.sequence` is the shot's identity everywhere else, and it is
            # what the director names when placing copy. Nothing renumbers it
            # per beat, so matching on the list position would drop copy from a
            # screenplay that numbers shots continuously across beats.
            placed_here = copy_by_position.get((beat.sequence, shot.sequence), ())
            present = beat_characters | {normalize_name(speaker)}
            applicable = tuple(
                item.text
                for item, characters, scenes in scoped
                if (not characters or characters & present)
                and (not scenes or beat.scene_key in scenes)
            )
            object_key = (
                normalize_name(shot.action.object) if shot.action and shot.action.object else ""
            )
            claims = (
                preserved
                if preserved and product_key and object_key == product_key
                else ()
            )
            result.append(
                ShotConstraints(
                    beat_sequence=beat.sequence,
                    shot_sequence=shot.sequence,
                    invariants=applicable,
                    product_claims=claims,
                    required_copy=tuple(placed_here),
                    prohibitions=forbidden,
                    prohibited_terms=forbidden_terms,
                )
            )
            _ = names
    return result


def anchor_keys_for_shot(shot: dict[str, Any], beat: dict[str, Any], product: str | None) -> list[str]:
    keys: list[str] = []
    actor = shot.get("speaker") or shot.get("actor")
    if actor:
        keys.append(f"character:{normalize_name(str(actor))}")
    location_key = str(beat.get("location_key") or "") or normalize_name(str(beat.get("location") or ""))
    if location_key:
        keys.append(f"scene:{location_key}")
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
