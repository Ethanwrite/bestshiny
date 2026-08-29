"""Beat planning and deterministic script rendering.

A beat plan is structured rows - beat intent, location, time, characters and
per-shot ``ShotIntent`` entries - and the script text is *derived* from them
for the existing deterministic ``NarrativeCompiler``. The renderer emits
exactly one primary visual action per line, in the compiler's own action
vocabulary, so the compiled shots map one-to-one onto the intents and the
intent extras (shot type, duration) can be applied to the real Shot rows
afterwards. Nothing here stores a grown prompt string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from platform_database import Database
from production_domain.models import CreativeFormat

_CJK = re.compile(r"[一-鿿]")

#: verb key -> (chinese rendering, english rendering). The verbs come from the
#: narrative compiler's ACTION_TERMS so every generated line parses into a
#: canonical action instead of the generic "act".
_VERBS: dict[str, tuple[str, str]] = {
    "enter": ("{name}进入{place}", "{name} enters the {place}"),
    "walk": ("{name}走向{target}", "{name} walks toward the {target}"),
    "look": ("{name}看向{target}", "{name} looks at the {target}"),
    "pick_up": ("{name}拿起{prop}", "{name} picks up the {prop}"),
    "raise": ("{name}举起{prop}", "{name} raises the {prop}"),
    "place": ("{name}放下{prop}", "{name} puts down the {prop}"),
    "turn": ("{name}转身", "{name} turns"),
    "stop": ("{name}停下", "{name} stops"),
    "sit": ("{name}坐下", "{name} sits"),
    "stand": ("{name}站起", "{name} stands"),
    "open": ("{name}打开{prop}", "{name} opens the {prop}"),
}

_EXTERIOR_CUES = (
    "天台",
    "屋顶",
    "街",
    "公园",
    "海",
    "山",
    "沙漠",
    "森林",
    "广场",
    "rooftop",
    "street",
    "park",
    "beach",
    "desert",
    "forest",
    "plaza",
    "outdoor",
)


@dataclass(frozen=True)
class ShotIntent:
    """One planned shot: a single primary action, structured."""

    action: str
    dialogue: str | None
    shot_type: str
    duration: float
    anchors: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "dialogue": self.dialogue,
            "shot_type": self.shot_type,
            "duration": self.duration,
            "anchors": list(self.anchors),
        }


@dataclass(frozen=True)
class PlannedBeat:
    sequence: int
    intent: str
    summary: str
    location: str
    time: str
    characters: tuple[str, ...]
    shots: tuple[ShotIntent, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "intent": self.intent,
            "summary": self.summary,
            "location": self.location,
            "time": self.time,
            "characters": list(self.characters),
            "shots": [shot.as_json() for shot in self.shots],
        }


def _is_cjk(value: str) -> bool:
    return bool(_CJK.search(value))


def action_line(name: str, verb: str, *, prop: str = "", target: str = "", place: str = "") -> str:
    chinese, english = _VERBS[verb]
    template = chinese if _is_cjk(name) else english
    place_value = place or "room"
    if not _is_cjk(name) and place_value[:1].isupper():
        # "enters the Tokyo" is not English; proper nouns drop the article.
        template = template.replace("the {place}", "{place}")
    return template.format(
        name=name, prop=prop or "phone", target=target or "door", place=place_value
    )


def dialogue_line(name: str, text: str) -> str:
    separator = "：" if _is_cjk(name) else ": "
    return f"{name}{separator}{text}"


#: Per-format beat scaffolds: (intent, essential, [(verb-or-DIALOGUE, shot_type)]).
#: Non-essential beats are dropped first when the duration budget is short.
_SCAFFOLDS: dict[str, list[tuple[str, bool, list[tuple[str, str]]]]] = {
    CreativeFormat.SHORT_DRAMA.value: [
        ("COLD_OPEN", True, [("enter", "WIDE"), ("DIALOGUE:hook", "CLOSE")]),
        ("SETUP", True, [("pick_up", "MEDIUM")]),
        ("TURN", True, [("look", "CLOSE"), ("DIALOGUE:turn", "CLOSE")]),
        ("ESCALATION", False, [("walk", "MEDIUM")]),
        ("CLIFFHANGER", True, [("stop", "CLOSE"), ("DIALOGUE:cliffhanger", "CLOSE")]),
    ],
    CreativeFormat.ADVERTISEMENT.value: [
        ("HOOK", True, [("enter", "WIDE")]),
        ("PROBLEM", True, [("DIALOGUE:pain", "MEDIUM")]),
        ("SOLUTION", True, [("raise", "CLOSE")]),
        ("PROOF", False, [("look", "CLOSE")]),
        ("CTA", True, [("DIALOGUE:cta", "MEDIUM")]),
    ],
    CreativeFormat.PRODUCT_SHOWCASE.value: [
        ("REVEAL", True, [("place", "WIDE")]),
        ("DETAIL", True, [("pick_up", "CLOSE")]),
        ("IN_USE", False, [("raise", "MEDIUM")]),
        ("PAYOFF", True, [("look", "CLOSE")]),
    ],
    CreativeFormat.SOCIAL_SHORT.value: [
        ("HOOK", True, [("enter", "MEDIUM")]),
        ("DEVELOP", False, [("pick_up", "CLOSE")]),
        ("PAYOFF", True, [("DIALOGUE:payoff", "CLOSE")]),
    ],
    CreativeFormat.MUSIC_VISUAL.value: [
        ("INTRO", True, [("enter", "WIDE")]),
        ("VERSE", True, [("walk", "MEDIUM")]),
        ("CHORUS", True, [("turn", "WIDE")]),
        ("BRIDGE", False, [("look", "CLOSE")]),
        ("OUTRO", True, [("stop", "WIDE")]),
    ],
    CreativeFormat.FASHION_LOOKBOOK.value: [
        ("OPENING_LOOK", True, [("enter", "WIDE")]),
        ("DETAIL_PASS", True, [("turn", "CLOSE")]),
        ("MOVEMENT", False, [("walk", "MEDIUM")]),
        ("FINALE", True, [("stop", "WIDE")]),
    ],
    CreativeFormat.BEAUTY_TUTORIAL.value: [
        ("BEFORE", True, [("look", "CLOSE")]),
        ("APPLICATION", True, [("pick_up", "CLOSE")]),
        ("TRANSFORM", False, [("turn", "MEDIUM")]),
        ("REVEAL", True, [("stand", "MEDIUM")]),
    ],
    CreativeFormat.CONCEPT_FILM.value: [
        ("FIRST_IMAGE", True, [("enter", "WIDE")]),
        ("DEVELOPMENT", True, [("walk", "MEDIUM")]),
        ("RUPTURE", False, [("turn", "CLOSE")]),
        ("RESOLUTION", True, [("stop", "WIDE")]),
    ],
}

_DIALOGUE_TEXT: dict[str, tuple[str, str]] = {
    "hook": ("你终于来了", "You finally came"),
    "turn": ("原来是你", "So it was you"),
    "cliffhanger": ("这件事还没有结束", "This is not over"),
    "pain": ("我受够了这个问题", "I am done with this problem"),
    "cta": ("现在就来试试", "Try it today"),
    "payoff": ("就是这个感觉", "That is the feeling"),
}


class BeatPlanner:
    """Deterministic beat scaffolding from an approved brief."""

    version = "creative-beats-v1"

    @staticmethod
    def _protagonist(fields: dict[str, Any]) -> str:
        characters = fields.get("characters") or []
        if characters and isinstance(characters, list):
            name = str(characters[0].get("name", "")).strip()
            if name:
                return name
        product = (fields.get("product") or {}).get("name")
        if product:
            return "Presenter" if not _is_cjk(str(product)) else "主理人"
        return "Alex"

    @staticmethod
    def _location(fields: dict[str, Any]) -> str:
        location = (fields.get("setting") or {}).get("location")
        return str(location).strip() if location else "studio"

    @staticmethod
    def _time(fields: dict[str, Any]) -> str:
        time_value = str((fields.get("setting") or {}).get("time") or "").upper()
        return "NIGHT" if time_value in {"NIGHT", "DUSK"} else "DAY"

    def plan(self, fields: dict[str, Any], *, format_value: str) -> list[PlannedBeat]:
        scaffold = _SCAFFOLDS.get(format_value) or _SCAFFOLDS[CreativeFormat.CONCEPT_FILM.value]
        duration = int(fields.get("duration_seconds") or 30)
        name = self._protagonist(fields)
        location = self._location(fields)
        time_of_day = self._time(fields)
        product = str((fields.get("product") or {}).get("name") or "").strip()
        prop = product if product else ("手机" if _is_cjk(name) else "phone")
        chinese = _is_cjk(name)

        # Shots run ~5 seconds each; keep every essential beat and drop the
        # optional ones when the target duration cannot carry them.
        beats = list(scaffold)
        while len(beats) > 1 and sum(len(shots) for _, _, shots in beats) * 5 > max(duration, 10):
            optional = next((index for index, beat in enumerate(beats) if not beat[1]), None)
            if optional is None:
                break
            beats.pop(optional)

        planned: list[PlannedBeat] = []
        for sequence, (intent, _essential, shot_specs) in enumerate(beats, 1):
            shots: list[ShotIntent] = []
            for verb_spec, shot_type in shot_specs:
                anchors = [f"character:{name}", f"scene:{location}"]
                if product:
                    anchors.append(f"product:{product}")
                if verb_spec.startswith("DIALOGUE:"):
                    text_key = verb_spec.split(":", 1)[1]
                    zh, en = _DIALOGUE_TEXT[text_key]
                    text = zh if chinese else en
                    shots.append(
                        ShotIntent(
                            action=dialogue_line(name, text),
                            dialogue=text,
                            shot_type="DIALOGUE",
                            duration=6.0,
                            anchors=tuple(anchors),
                        )
                    )
                else:
                    shots.append(
                        ShotIntent(
                            action=action_line(
                                name, verb_spec, prop=prop, target=prop, place=location
                            ),
                            dialogue=None,
                            shot_type=shot_type,
                            duration=5.0,
                            anchors=tuple(anchors),
                        )
                    )
            planned.append(
                PlannedBeat(
                    sequence=sequence,
                    intent=intent,
                    summary=f"{intent.replace('_', ' ').title()} - {name} at {location}",
                    location=location,
                    time=time_of_day,
                    characters=(name,),
                    shots=tuple(shots),
                )
            )
        return planned


def _scene_prefix(location: str) -> str:
    lowered = location.casefold()
    return "EXT." if any(cue in lowered for cue in _EXTERIOR_CUES) else "INT."


def render_script(beats: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Render beats to compiler-ready script text plus the aligned intents.

    Returns the script and the flat shot-intent list in the exact order the
    narrative compiler will create shots for it, so callers can zip compiled
    shot ids with intents and apply the structured extras.
    """

    lines: list[str] = []
    ordered_intents: list[dict[str, Any]] = []
    current_scene: tuple[str, str] | None = None
    for beat in beats:
        location = str(beat.get("location") or "studio")
        time_of_day = str(beat.get("time") or "DAY").upper()
        if time_of_day not in {"DAY", "NIGHT"}:
            time_of_day = "DAY"
        scene_key = (location.casefold(), time_of_day)
        if scene_key != current_scene:
            lines.append(f"{_scene_prefix(location)} {location} - {time_of_day}")
            current_scene = scene_key
        for shot in beat.get("shots", []):
            action = str(shot.get("action") or "").strip()
            if not action:
                continue
            lines.append(action)
            ordered_intents.append(dict(shot))
    return "\n".join(lines), ordered_intents


class ShotIntentMismatch(ValueError):
    """The compiled shot list and the rendered intents disagree."""


def apply_shot_intents(
    database: Database, shot_ids: list[str], intents: list[dict[str, Any]]
) -> None:
    """Apply structured extras onto compiled shots, by position.

    The renderer emitted exactly one action line per intent and the narrative
    compiler creates exactly one shot per line, so ordinal zip is the honest
    mapping; a length mismatch means the two disagree and is raised rather
    than silently trimmed.
    """

    if len(shot_ids) != len(intents):
        raise ShotIntentMismatch(
            f"compiled {len(shot_ids)} shots for {len(intents)} shot intents; "
            "the beat renderer and narrative compiler disagree"
        )
    from production_domain.models import Shot

    with database.session() as session:
        for shot_id, intent in zip(shot_ids, intents, strict=True):
            shot = session.get(Shot, shot_id)
            if shot is None:
                continue
            shot_type = str(intent.get("shot_type") or "").strip()
            if shot_type and shot.shot_type != "DIALOGUE":
                shot.shot_type = shot_type
            duration = intent.get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                shot.duration = float(duration)
        session.flush()
