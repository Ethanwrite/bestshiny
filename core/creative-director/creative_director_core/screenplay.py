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
    DIRECTOR_FORBIDDEN_KEYS,
    MAX_CAST,
    MAX_PROP_ANCHORS,
    MAX_SCENE_ANCHORS,
    SHOT_PLANNER_FORBIDDEN_BEAT_KEYS,
    SHOT_PLANNER_FORBIDDEN_SHOT_KEYS,
    SHOT_PLANNER_FORBIDDEN_TOP_KEYS,
    Screenplay,
    ShotPlan,
    StoryDraft,
    normalize_name,
    strip_forbidden_keys,
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
            # The staging the prompt compiler reads, identical for both shapes:
            # who is in frame, who must be recognisable, what may move.
            staging = {
                "micro_actions": list(shot.micro_actions),
                "present_characters": [
                    names.get(normalize_name(name), name) for name in shot.present_characters
                ],
                "identity_critical_characters": [
                    names.get(normalize_name(name), name)
                    for name in shot.identity_critical_characters
                ],
            }
            if shot.action is None:
                assert shot.dialogue is not None
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
                        **staging,
                    }
                )
                continue
            action = shot.action
            actor = names.get(normalize_name(action.actor), action.actor)
            rendered = action_line(
                script_name(actor),
                action.verb,
                prop=_clean_phrase(action.object),
                target=_clean_phrase(action.target),
                place=location,
            )
            # The rendered line carries the dominant action for the narrative
            # compiler; a line spoken during it rides beside, as `dialogue` and
            # `speaker`, and reaches the prompt through the director intent.
            spoken = (
                {
                    "dialogue": shot.dialogue.text,
                    "speaker": names.get(normalize_name(shot.dialogue.speaker), shot.dialogue.speaker),
                }
                if shot.dialogue is not None
                else {"dialogue": None}
            )
            shots.append(
                {
                    "sequence": shot.sequence,
                    "action": rendered,
                    **spoken,
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
                    **staging,
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
                    if key == "shot_type" and shot.get("dialogue") and not shot.get("action"):
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
            for name in (*shot.named_characters, *shot.present_characters):
                appearing.add(normalize_name(name))
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
            # `shot.sequence` is the shot's identity everywhere else, and it is
            # what the director names when placing copy. Nothing renumbers it
            # per beat, so matching on the list position would drop copy from a
            # screenplay that numbers shots continuously across beats.
            placed_here = copy_by_position.get((beat.sequence, shot.sequence), ())
            present = beat_characters | {
                normalize_name(name) for name in (*shot.named_characters, *shot.present_characters)
            }
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
    """The key visuals a rendered shot implies.

    Character anchors are the shot's identity-critical characters - the faces
    the provider is given as references - never everyone present: a present
    character is staged in the prompt, an identity-critical one is bound to a
    reference plate. Without the declaration (older beat plans) the actor or
    speaker is the one face.
    """

    keys: list[str] = []
    critical = [str(name) for name in (shot.get("identity_critical_characters") or []) if str(name)]
    if not critical:
        actor = shot.get("speaker") or shot.get("actor")
        critical = [str(actor)] if actor else []
    for name in critical:
        key = f"character:{normalize_name(name)}"
        if key not in keys:
            keys.append(key)
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


# ------------------------------------------------- two-stage authoring
def story_from_screenplay(content: dict[str, Any]) -> dict[str, Any]:
    """A screenplay's story half: everything the Director owns, no shots.

    Beat dialogue is reconstructed from the shots in shot order, so a
    revision request hands the Director exactly the lines it wrote.
    """

    story = {key: value for key, value in dict(content).items() if key not in {"beats", "_context"}}
    beats: list[dict[str, Any]] = []
    for beat in content.get("beats") or []:
        if not isinstance(beat, dict):
            continue
        lines = [
            dict(shot["dialogue"])
            for shot in sorted(
                (item for item in (beat.get("shots") or []) if isinstance(item, dict)),
                key=lambda item: int(item.get("sequence") or 0),
            )
            if isinstance(shot.get("dialogue"), dict) and shot["dialogue"].get("text")
        ]
        beats.append(
            {
                "sequence": beat.get("sequence"),
                "intent": beat.get("intent"),
                "summary": beat.get("summary", ""),
                "scene_key": beat.get("scene_key"),
                "characters": list(beat.get("characters") or []),
                "emotional_beat": beat.get("emotional_beat", ""),
                "dialogue": lines,
            }
        )
    story["beats"] = beats
    # Copy placement is the Shot Planner's; the story keeps the beat only.
    story["required_copy"] = [
        {"text": item.get("text"), "beat": item.get("beat"), "shot": None}
        if isinstance(item, dict)
        else item
        for item in (content.get("required_copy") or [])
    ]
    return story


def shot_plan_from_screenplay(content: dict[str, Any]) -> dict[str, Any]:
    """A screenplay's shot half, in the Shot Planner's own contract."""

    return {
        "beats": [
            {"sequence": beat.get("sequence"), "shots": [dict(shot) for shot in beat.get("shots") or []]}
            for beat in (content.get("beats") or [])
            if isinstance(beat, dict)
        ],
        "required_copy": [
            dict(item) for item in (content.get("required_copy") or []) if isinstance(item, dict)
        ],
        "mobile_hook_check": "",
        "unresolved": [],
    }


def validate_story(payload: Any) -> tuple[StoryDraft, list[str]]:
    """Validate the Director's story; out-of-authority keys are stripped and named."""

    if not isinstance(payload, dict):
        raise ScreenplayInvalid("story must be a JSON object", ["root is not an object"])
    cleaned, stripped = strip_forbidden_keys(
        payload, DIRECTOR_FORBIDDEN_KEYS, recurse_into=("beats",)
    )
    try:
        story = StoryDraft.model_validate(cleaned)
    except ValidationError as exc:
        details = [
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg')}"
            for error in exc.errors()
        ][:20]
        raise ScreenplayInvalid("story failed validation", details) from exc
    by_script_name: dict[str, str] = {}
    problems: list[str] = []
    for character in story.characters:
        token = script_name(character.name).casefold()
        other = by_script_name.get(token)
        if other is not None and other != character.name:
            problems.append(
                f"characters {other!r} and {character.name!r} collapse to the same script name "
                f"{script_name(character.name)!r}; rename one of them"
            )
        by_script_name.setdefault(token, character.name)
    if problems:
        raise ScreenplayInvalid("story failed the cast contract", problems[:20])
    return story, stripped


def validate_shot_plan(payload: Any) -> tuple[ShotPlan, list[str]]:
    """Validate the Shot Planner's plan; photographic and story keys are stripped and named."""

    if not isinstance(payload, dict):
        raise ScreenplayInvalid("shot plan must be a JSON object", ["root is not an object"])
    cleaned, stripped = strip_forbidden_keys(payload, SHOT_PLANNER_FORBIDDEN_TOP_KEYS)
    beats: list[Any] = []
    for index, beat in enumerate(cleaned.get("beats") or []):
        if not isinstance(beat, dict):
            beats.append(beat)
            continue
        beat_clean, beat_stripped = strip_forbidden_keys(
            beat,
            SHOT_PLANNER_FORBIDDEN_BEAT_KEYS,
            path=f"beats[{index}]",
        )
        stripped.extend(beat_stripped)
        shots: list[Any] = []
        for shot_index, shot in enumerate(beat_clean.get("shots") or []):
            if not isinstance(shot, dict):
                shots.append(shot)
                continue
            shot_clean, shot_stripped = strip_forbidden_keys(
                shot,
                SHOT_PLANNER_FORBIDDEN_SHOT_KEYS,
                path=f"beats[{index}].shots[{shot_index}]",
            )
            stripped.extend(shot_stripped)
            shots.append(shot_clean)
        beat_clean["shots"] = shots
        beats.append(beat_clean)
    cleaned["beats"] = beats
    try:
        plan = ShotPlan.model_validate(cleaned)
    except ValidationError as exc:
        details = [
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg')}"
            for error in exc.errors()
        ][:20]
        raise ScreenplayInvalid("shot plan failed validation", details) from exc
    return plan, stripped


def _line_key(speaker: str, text: str) -> tuple[str, str]:
    return normalize_name(speaker), " ".join(str(text).split())


def shot_plan_violations(story: StoryDraft, plan: ShotPlan) -> list[str]:
    """Where the plan stepped on the Director's authority, or left the story unplanned.

    The Shot Planner places dialogue; it may not write, drop or reword a
    line. It stages the Director's characters; it may not invent one. Every
    beat must be planned, and no beat may be planned that the story does not
    have. Any finding here rejects the plan as a whole.
    """

    problems: list[str] = []
    story_beats = {beat.sequence: beat for beat in story.beats}
    names = {normalize_name(character.name) for character in story.characters}
    planned = {beat.sequence: beat for beat in plan.beats}
    for sequence in sorted(story_beats):
        if sequence not in planned:
            problems.append(f"beat_unplanned:{sequence}")
    for sequence, beat in sorted(planned.items()):
        story_beat = story_beats.get(sequence)
        if story_beat is None:
            problems.append(f"beat_invented:{sequence}")
            continue
        expected = [_line_key(line.speaker, line.text) for line in story_beat.dialogue]
        placed = [
            _line_key(shot.dialogue.speaker, shot.dialogue.text)
            for shot in sorted(beat.shots, key=lambda item: item.sequence)
            if shot.dialogue is not None
        ]
        if placed != expected:
            missing = [item for item in expected if item not in placed]
            extra = [item for item in placed if item not in expected]
            if missing and extra and len(missing) == len(extra):
                problems.append(f"dialogue_changed:beat {sequence}")
            elif missing:
                problems.append(f"dialogue_dropped:beat {sequence}")
            elif extra:
                problems.append(f"dialogue_invented:beat {sequence}")
            else:
                problems.append(f"dialogue_reordered:beat {sequence}")
        for shot in beat.shots:
            for name in (*shot.named_characters, *shot.present_characters):
                if normalize_name(name) not in names:
                    problems.append(f"unknown_character:{name}")
    story_copy = {" ".join(item.text.split()) for item in story.required_copy}
    for item in plan.required_copy:
        if " ".join(item.text.split()) not in story_copy:
            problems.append(f"required_copy_changed:{item.text[:40]}")
    return list(dict.fromkeys(problems))


def merge_story_and_shot_plan(story: StoryDraft, plan: ShotPlan) -> Screenplay:
    """One screenplay from the two stages; the plan must already be clean."""

    violations = shot_plan_violations(story, plan)
    if violations:
        raise ScreenplayInvalid("shot plan violates the story's authority", violations)
    planned = {beat.sequence: beat for beat in plan.beats}
    placements = {
        " ".join(item.text.split()): item for item in plan.required_copy if item.beat is not None
    }
    content = story.model_dump(by_alias=True)
    beats: list[dict[str, Any]] = []
    for beat in story.beats:
        shots: list[dict[str, Any]] = []
        for shot in sorted(planned[beat.sequence].shots, key=lambda item: item.sequence):
            shot_json = shot.model_dump(by_alias=True)
            # Shot size is Cinematography's decision; the planner hands on a
            # kind, not a framing. DIALOGUE is a kind (a speaking shot).
            shot_json["shot_type"] = "DIALOGUE" if shot.action is None else "MEDIUM"
            shots.append(shot_json)
        beat_json = beat.model_dump(by_alias=True)
        beat_json.pop("dialogue", None)
        beat_json["shots"] = shots
        beats.append(beat_json)
    content["beats"] = beats
    required_copy: list[dict[str, Any]] = []
    for item in story.required_copy:
        placement = placements.get(" ".join(item.text.split()))
        required_copy.append(
            {
                "text": item.text,
                "beat": placement.beat if placement is not None else item.beat,
                "shot": placement.shot if placement is not None else item.shot,
            }
        )
    content["required_copy"] = required_copy
    content["unresolved"] = list(dict.fromkeys([*story.unresolved, *plan.unresolved]))
    return validate_screenplay(content)


def _placeholder_duration(text: str) -> float:
    from .schemas import (
        DIALOGUE_LEAD_SECONDS,
        DIALOGUE_TAIL_SECONDS,
        MAX_SHOT_DURATION_SECONDS,
        MIN_SHOT_DURATION_SECONDS,
        estimated_speech_seconds,
    )

    needed = estimated_speech_seconds(text) + DIALOGUE_LEAD_SECONDS + DIALOGUE_TAIL_SECONDS
    return float(min(MAX_SHOT_DURATION_SECONDS, max(MIN_SHOT_DURATION_SECONDS, 3.0, needed + 0.5)))


def deterministic_shot_plan(story: StoryDraft, *, reason: str) -> ShotPlan:
    """The labelled degradation of the shot stage: the story's lines, one per shot.

    Every required line becomes a speaking shot; a beat with no line gets one
    placeholder look. Nothing here is the planner's staging, and the plan says
    so in ``unresolved`` so the screenplay carries the label.
    """

    beats: list[dict[str, Any]] = []
    for beat in story.beats:
        shots: list[dict[str, Any]] = []
        for line in beat.dialogue:
            shots.append(
                {
                    "sequence": len(shots) + 1,
                    "duration": _placeholder_duration(line.text),
                    "dialogue": {"speaker": line.speaker, "text": line.text},
                    "start_state": "",
                    "end_state": "",
                    "gaze_target": "",
                }
            )
        if not shots:
            actor = beat.characters[0] if beat.characters else story.characters[0].name
            shots.append(
                {
                    "sequence": 1,
                    "duration": 4.0,
                    "action": {
                        "actor": actor,
                        "verb": "look",
                        "target": "",
                        "description": "placeholder staging; the shot planner model was unavailable",
                    },
                    "start_state": "",
                    "end_state": "",
                    "gaze_target": "",
                }
            )
        beats.append({"sequence": beat.sequence, "shots": shots})
    return ShotPlan.model_validate(
        {
            "beats": beats,
            "required_copy": [],
            "mobile_hook_check": "",
            "unresolved": [
                f"DETERMINISTIC SHOT PLAN: the shot planner model was unavailable ({reason}). "
                "One line per shot with no staging; redraft before approving, or approve knowing this."
            ],
        }
    )


# ------------------------------------------------------ invariant versions
#: The invariant classes the Director locks: a change to any of them is a new
#: approved version that supersedes the previous one, never an overwrite.
INVARIANT_CLASSES: tuple[str, ...] = (
    "identity",
    "relationships",
    "scene_geography",
    "required_dialogue",
    "ending",
    "invariants",
    "product_claims",
    "required_copy",
    "obligations",
)


def invariant_classes(content: dict[str, Any]) -> dict[str, Any]:
    """The invariant-class subset of a screenplay, in a canonical shape."""

    characters = sorted(
        (
            {
                "name": normalize_name(str(item.get("name") or "")),
                "role": str(item.get("role") or ""),
                "look": str(item.get("look") or ""),
            }
            for item in (content.get("characters") or [])
            if isinstance(item, dict)
        ),
        key=lambda item: item["name"],
    )
    relationships = sorted(
        f"{normalize_name(str(item.get('name') or ''))}|{normalize_name(str(rel.get('with') or ''))}|"
        f"{str(rel.get('relation') or '')}"
        for item in (content.get("characters") or [])
        if isinstance(item, dict)
        for rel in (item.get("relationships") or [])
        if isinstance(rel, dict)
    )
    scenes = sorted(
        f"{item.get('key')}|{normalize_name(str(item.get('location') or ''))}|{item.get('time')}|"
        f"{item.get('interior')}"
        for item in (content.get("scenes") or [])
        if isinstance(item, dict)
    )
    dialogue = [
        f"{beat.get('sequence')}|{normalize_name(str(shot['dialogue'].get('speaker') or ''))}|"
        f"{' '.join(str(shot['dialogue'].get('text') or '').split())}"
        for beat in (content.get("beats") or [])
        if isinstance(beat, dict)
        for shot in sorted(
            (item for item in (beat.get("shots") or []) if isinstance(item, dict)),
            key=lambda item: int(item.get("sequence") or 0),
        )
        if isinstance(shot.get("dialogue"), dict)
    ]
    treatment = content.get("treatment") if isinstance(content.get("treatment"), dict) else {}
    return {
        "identity": characters,
        "relationships": relationships,
        "scene_geography": scenes,
        "required_dialogue": dialogue,
        "ending": " ".join(str(treatment.get("ending") or "").split()),
        "invariants": sorted(
            json.dumps(item, sort_keys=True, ensure_ascii=False) if isinstance(item, dict) else str(item)
            for item in (content.get("invariants") or [])
        ),
        "product_claims": sorted(
            str(item.get("claim") if isinstance(item, dict) else item)
            for item in (content.get("product_claims") or [])
        ),
        "required_copy": sorted(
            " ".join(str(item.get("text") if isinstance(item, dict) else item).split())
            for item in (content.get("required_copy") or [])
        ),
        "obligations": sorted(
            f"{item.get('key')}|{item.get('promise')}"
            for item in (content.get("obligations") or [])
            if isinstance(item, dict)
        ),
    }


def invariant_version(content: dict[str, Any]) -> str:
    """A content-addressed version of the invariant classes alone."""

    encoded = json.dumps(
        invariant_classes(_without_audit(content)), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return "inv:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def invariant_changes(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    """Which invariant classes differ between two screenplay contents."""

    if previous is None:
        return []
    before = invariant_classes(_without_audit(previous))
    after = invariant_classes(_without_audit(current))
    return [name for name in INVARIANT_CLASSES if before.get(name) != after.get(name)]


def invariant_record(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """What a revision records about its invariants: version, what it supersedes, what moved."""

    version = invariant_version(current)
    previous_version = invariant_version(previous) if previous is not None else None
    changed = invariant_changes(previous, current)
    return {
        "version": version,
        "supersedes_version": previous_version if changed else None,
        "unchanged_from_version": previous_version if previous is not None and not changed else None,
        "changed": changed,
    }


def _without_audit(content: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(content or {}).items() if key != "_context"}
