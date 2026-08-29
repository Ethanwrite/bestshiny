from __future__ import annotations

import hashlib
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from platform_database import Database
from production_domain.models import (
    Character,
    Episode,
    Location,
    NarrativeEvent,
    NarrativeFact,
    NarrativeObligation,
    Prop,
    Scene,
    Shot,
    ShotDependency,
    ShotDependencyOrigin,
    ShotDependencyType,
    ShotNarrativeEffect,
    ShotNarrativeEffectOrigin,
    ShotNarrativeEffectType,
    ShotStatus,
    TimelineState,
    TimelineTransition,
    TimelineTransitionType,
)
from sqlalchemy import delete, func, select

SCENE_HEADER = re.compile(
    r"^(?:(INT\.?|EXT\.?|内景|外景)\s*)?(.+?)(?:\s*[-—]\s*(DAY|NIGHT|日|夜|黄昏|清晨|雨夜))?$",
    re.IGNORECASE,
)
DIALOGUE = re.compile(r"^([\w\u4e00-\u9fff][\w\u4e00-\u9fff ·]{0,23})[:：]\s*(.+)$")
SCENE_PREFIXES = ("INT.", "EXT.", "INT ", "EXT ", "内景", "外景", "场景")
ACTION_SPLIT = re.compile(
    r"\s*(?:[。；;]+|(?<=[A-Za-z])\.\s+|[，,]?\s*(?:然后|随后|接着|继而)\s*|"
    r"\s+(?:and\s+then|then|afterwards)\s+)\s*",
    re.IGNORECASE,
)
MINOR_REACTION = re.compile(r"\s+(?:while|as)\s+|\s*(?:同时|与此同时)\s*", re.IGNORECASE)

ACTION_TERMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("举起", "raises", "raise"), "raise"),
    (("拿起", "picks up", "pick up"), "pick_up"),
    (("放下", "puts down", "place"), "place"),
    (("打开", "opens", "open"), "open"),
    (("关上", "closes", "close"), "close"),
    (("转身", "turns", "turn"), "turn"),
    (("看向", "looks", "look"), "look"),
    (("走向", "walks", "walk"), "walk"),
    (("进入", "enters", "enter"), "enter"),
    (("离开", "exits", "leaves"), "exit"),
    (("推", "pushes", "push"), "push"),
    (("拉", "pulls", "pull"), "pull"),
    (("坐下", "sits", "sit"), "sit"),
    (("站起", "stands", "stand"), "stand"),
    (("停下", "stops", "stop"), "stop"),
)

#: Explicit narrative-effect directives. A rules compiler cannot *derive*
#: story meaning, but it can carry meaning the author declared: these lines
#: never become beats — each attaches to the next action/dialogue line's shot
#: (or the previous shot when trailing) and compiles into declared ledger
#: effects plus the explicit dependencies they imply.
#:
#:   [ESTABLISH fact_key: summary]              fact becomes true in this shot
#:   [ESTABLISH fact_key: summary -> Mira]      ... and Mira witnesses it
#:   [DISCLOSE fact_key -> Mira, AUDIENCE]      holders learn an earlier fact
#:   [FORESHADOW obligation_key: promise]       opens an obligation (setup)
#:   [PAYOFF obligation_key]                    settles it (payoff)
#:   [PAYOFF obligation_key: reason]
DIRECTIVE = re.compile(r"^\[(ESTABLISH|DISCLOSE|FORESHADOW|PAYOFF)\s+([^\]]+)\]$", re.IGNORECASE)
DIRECTIVE_KEY = re.compile(r"^[A-Za-z0-9_.\-]{1,160}$")

PROP_TERMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("手机", "phone"), "phone"),
    (("门", "door"), "door"),
    (("杯子", "glass", "cup"), "cup"),
    (("信封", "envelope"), "envelope"),
    (("钥匙", "key"), "key"),
    (("包", "bag"), "bag"),
    (("枪", "gun"), "gun"),
)


@dataclass(frozen=True)
class CompileResult:
    episode_id: str
    scene_ids: list[str]
    shot_ids: list[str]
    event_ids: list[str]
    entities: dict[str, list[str]]
    script_hash: str


class NarrativeCompiler:
    """Compile script text into a deterministic entity/event/state graph.

    This is deliberately a rules compiler, not an LLM shot splitter. SQL
    ``TimelineState`` rows remain the authoritative runtime state; the JSON
    graph on the episode is an inspectable compilation artifact.
    """

    version = "narrative-rules-v3"

    def __init__(self, database: Database):
        self.database = database

    @classmethod
    def _parse_directive(cls, line: str) -> dict[str, Any] | None:
        """One `[KEYWORD ...]` line, or ``None`` when the line is not one.

        A line that *looks* like a directive but cannot be parsed raises: a
        silently mis-parsed declaration would compile into a shot prompt, and
        the author's narrative bookkeeping would vanish without a trace.
        """

        match = DIRECTIVE.match(line)
        if not match:
            return None
        keyword = match.group(1).upper()
        body = match.group(2).strip()

        def _key(value: str) -> str:
            value = value.strip()
            if not DIRECTIVE_KEY.match(value):
                raise ValueError(
                    f"invalid {keyword} key {value!r}: keys are 1-160 chars of "
                    "letters, digits, '_', '.', '-'"
                )
            return value

        def _holders(value: str) -> list[str]:
            names = [name.strip() for name in re.split(r"[,、]", value) if name.strip()]
            if not names:
                raise ValueError(f"{keyword} directive names no holder: {line!r}")
            return names

        if keyword == "ESTABLISH":
            head, _, holder_part = body.partition("->")
            key_part, sep, summary = head.partition(":")
            if not sep and "：" in head:
                key_part, _, summary = head.partition("：")
            if not summary.strip():
                raise ValueError(f"ESTABLISH requires 'key: summary': {line!r}")
            return {
                "keyword": keyword,
                "fact_key": _key(key_part),
                "summary": summary.strip(),
                "holders": _holders(holder_part) if holder_part.strip() else [],
                "source": line,
            }
        if keyword == "DISCLOSE":
            key_part, sep, holder_part = body.partition("->")
            if not sep or not holder_part.strip():
                raise ValueError(f"DISCLOSE requires 'key -> holder[, holder]': {line!r}")
            return {
                "keyword": keyword,
                "fact_key": _key(key_part),
                "holders": _holders(holder_part),
                "source": line,
            }
        if keyword == "FORESHADOW":
            key_part, sep, promise = body.partition(":")
            if not sep and "：" in body:
                key_part, _, promise = body.partition("：")
            if not promise.strip():
                raise ValueError(f"FORESHADOW requires 'key: promise': {line!r}")
            return {
                "keyword": keyword,
                "obligation_key": _key(key_part),
                "promise": promise.strip(),
                "source": line,
            }
        # PAYOFF
        key_part, _, reason = body.partition(":")
        return {
            "keyword": keyword,
            "obligation_key": _key(key_part),
            "reason": reason.strip(),
            "source": line,
        }

    @staticmethod
    def _lines(script: str) -> list[str]:
        return [line.strip() for line in script.replace("\r\n", "\n").split("\n") if line.strip()]

    @staticmethod
    def _is_scene_header(line: str) -> bool:
        upper = line.upper()
        return upper.startswith(SCENE_PREFIXES) or line.startswith(("第", "场")) and "场" in line[:6]

    @staticmethod
    def _scene_parts(header: str, fallback: int) -> tuple[str, str]:
        cleaned = header
        if cleaned.startswith("场景"):
            cleaned = cleaned.removeprefix("场景").lstrip(" ：:")
        match = SCENE_HEADER.match(cleaned)
        if not match:
            return f"Scene {fallback}", ""
        location = match.group(2).strip(" -—") or f"Scene {fallback}"
        return location, (match.group(3) or "").lower()

    @staticmethod
    def _stable_id(scope: str, kind: str, value: str) -> str:
        normalized = " ".join(value.casefold().split())
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ai-director:{scope}:{kind}:{normalized}"))

    @staticmethod
    def _split_primary_actions(line: str) -> list[str]:
        if DIALOGUE.match(line):
            return [line]
        values = [value.strip(" ,，") for value in ACTION_SPLIT.split(line) if value.strip(" ,，")]
        return values or [line]

    @staticmethod
    def _canonical_action(source: str) -> str:
        lowered = source.casefold()
        for terms, action in ACTION_TERMS:
            if any(term.casefold() in lowered for term in terms):
                return action
        return "act"

    @staticmethod
    def _named_prop(source: str) -> str | None:
        lowered = source.casefold()
        for terms, canonical_name in PROP_TERMS:
            if any(term.casefold() in lowered for term in terms):
                return canonical_name
        return None

    @staticmethod
    def _actor(source: str) -> str | None:
        latin = re.match(r"^([A-Z][A-Za-z0-9_-]{1,30})(?:\s+|(?=[A-Z][a-z]+s\b))", source)
        chinese = re.match(
            r"^([\u4e00-\u9fff]{2,4}?)(?=举起|抬|走|转|看|拿|放|推|拉|站|坐|说|跑|停|打开|关上|进入|离开)",
            source,
        )
        if latin:
            return latin.group(1)
        if chinese:
            return chinese.group(1)
        return None

    @staticmethod
    def _target(source: str, actor: str | None) -> str | None:
        chinese = re.search(r"(?:给|向|朝)([\u4e00-\u9fff]{2,4}?)(?=看|走|转|展示|说|。|，|$)", source)
        latin = re.search(
            r"(?i:toward|towards|to|at|shows?)\s+([A-Z][A-Za-z0-9_-]{1,30})\b",
            source,
        )
        target = chinese.group(1) if chinese else latin.group(1) if latin else None
        if target and actor and target.casefold() == actor.casefold():
            return None
        return target

    @classmethod
    def _event(cls, line: str, sequence: int) -> dict[str, Any]:
        dialogue = DIALOGUE.match(line)
        if dialogue:
            actor, words = dialogue.groups()
            return {
                "actor": actor.strip(),
                "action": "speak",
                "target": None,
                "object": None,
                "dialogue": words.strip(),
                "source": line,
                "minor_reaction": None,
                "sequence": sequence,
            }
        parts = MINOR_REACTION.split(line, maxsplit=1)
        primary = parts[0].strip()
        actor = cls._actor(primary)
        return {
            "actor": actor,
            "action": cls._canonical_action(primary),
            "target": cls._target(primary, actor),
            "object": cls._named_prop(primary),
            "dialogue": "",
            "source": primary,
            "minor_reaction": parts[1].strip() if len(parts) == 2 else None,
            "sequence": sequence,
        }

    @staticmethod
    def _base_state(location: Location, time_context: str) -> dict[str, Any]:
        return {
            "scene": {
                "location_id": location.id,
                "location": location.name,
                "time": time_context,
            },
            "characters": {},
            "props": {},
            "positions": {},
            "orientation": {},
            "pose": {},
            "costume": {},
            "held_props": {},
            "lighting": {"continuity": "scene_default"},
            "camera": {"axis": "A", "angle": "eye_level", "shot_size": "medium"},
            "narrative_facts": [],
            # Retained for V1 prompt/compiler compatibility.
            "important_facts": [],
        }

    @staticmethod
    def _apply_event(
        state: dict[str, Any],
        parsed: dict[str, Any],
        *,
        event_id: str,
        actor_id: str | None,
        target_id: str | None,
        object_id: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        post_state = deepcopy(state)
        changes: dict[str, Any] = {
            "event_id": event_id,
            "primary_action": parsed["action"],
            "source": parsed["source"],
        }
        if actor_id:
            character = post_state["characters"].setdefault(
                actor_id,
                {
                    "name": parsed["actor"],
                    "position": "midground_center",
                    "orientation": "scene_partner",
                    "pose": "neutral",
                    "costume": "canonical",
                    "visible": True,
                    "gaze_target": target_id or object_id or "scene_action",
                },
            )
            character["last_action"] = parsed["action"]
            character["gaze_target"] = target_id or object_id or character.get("gaze_target", "scene_action")
            post_state["positions"].setdefault(actor_id, character["position"])
            post_state["orientation"].setdefault(actor_id, character["orientation"])
            post_state["pose"][actor_id] = parsed["action"]
            post_state["costume"].setdefault(actor_id, "canonical")
            changes[f"characters.{actor_id}.last_action"] = parsed["action"]
            changes[f"characters.{actor_id}.gaze_target"] = character["gaze_target"]
        if object_id:
            prop = post_state["props"].setdefault(
                object_id,
                {"name": parsed["object"], "visibility": "visible", "holder": None},
            )
            if actor_id and parsed["action"] in {"raise", "pick_up"}:
                post_state["held_props"][actor_id] = {"right_hand": object_id}
                prop["holder"] = actor_id
                changes[f"held_props.{actor_id}.right_hand"] = object_id
            elif actor_id and parsed["action"] == "place":
                post_state["held_props"].setdefault(actor_id, {})["right_hand"] = None
                prop["holder"] = None
                changes[f"held_props.{actor_id}.right_hand"] = None
        fact = {
            "event_id": event_id,
            "sequence": parsed["sequence"],
            "fact": parsed["source"],
        }
        post_state["narrative_facts"] = [*post_state.get("narrative_facts", []), fact]
        post_state["important_facts"] = [
            *post_state.get("important_facts", []),
            parsed["source"],
        ]
        post_state["last_event"] = parsed["action"]
        post_state["primary_action"] = parsed["action"]
        if parsed["minor_reaction"]:
            post_state["minor_reaction"] = parsed["minor_reaction"]
            changes["minor_reaction"] = parsed["minor_reaction"]
        return post_state, changes

    @staticmethod
    def _entity_row_map(rows: list[Any]) -> dict[str, Any]:
        return {str(row.name).casefold(): row for row in rows}

    def _apply_shot_directives(
        self,
        session: Any,
        *,
        episode: Episode,
        shot: Shot,
        scene_sequence: int,
        shot_sequence: int,
        directives: list[dict[str, Any]],
        characters: dict[str, Character],
        registry: dict[str, dict[str, Shot]],
    ) -> None:
        """Compile the shot's declared directives into effects and dependencies.

        Effects become ledger writes when the shot commits; DISCLOSE and PAYOFF
        also declare the FACT_REVELATION / OBLIGATION_FULFILLMENT /
        FORESHADOWING dependencies that force the earlier material into this
        shot's generation context. All referent validation happens here, at
        compile time, fail-closed.
        """

        project_id = episode.project_id
        position = (episode.episode_number, scene_sequence, shot_sequence)

        def _holder_keys(names: list[str]) -> list[str]:
            keys: list[str] = []
            for name in names:
                if name.upper() == "AUDIENCE":
                    keys.append("AUDIENCE")
                    continue
                row = characters.get(name.casefold())
                if row is None:
                    raise ValueError(f"directive holder is not a known character: {name!r}")
                keys.append(row.id)
            return list(dict.fromkeys(keys))

        def _declare_effect(**kwargs: Any) -> None:
            effect_key = ShotNarrativeEffect.natural_key(
                kwargs["effect_type"],
                fact_key=kwargs.get("fact_key"),
                obligation_key=kwargs.get("obligation_key"),
                holder_key=kwargs.get("holder_key"),
            )
            session.add(
                ShotNarrativeEffect(
                    project_id=project_id,
                    shot_id=shot.id,
                    episode_number=position[0],
                    scene_sequence=position[1],
                    shot_sequence=position[2],
                    origin=ShotNarrativeEffectOrigin.SCRIPT_COMPILER.value,
                    effect_key=effect_key,
                    **kwargs,
                )
            )

        def _declare_dependency(
            dependency_type: str,
            *,
            source_shot_id: str | None = None,
            fact_key: str | None = None,
            obligation_key: str | None = None,
            summary: str = "",
        ) -> None:
            dependency_key = ShotDependency.natural_key(
                dependency_type,
                source_shot_id=source_shot_id,
                fact_key=fact_key,
                obligation_key=obligation_key,
            )
            exists = session.scalar(
                select(ShotDependency.id).where(
                    ShotDependency.target_shot_id == shot.id,
                    ShotDependency.dependency_key == dependency_key,
                )
            )
            if exists is None:
                session.add(
                    ShotDependency(
                        project_id=project_id,
                        target_shot_id=shot.id,
                        source_shot_id=source_shot_id,
                        dependency_type=dependency_type,
                        fact_key=fact_key,
                        obligation_key=obligation_key,
                        summary=summary,
                        origin=ShotDependencyOrigin.SCRIPT_COMPILER.value,
                        dependency_key=dependency_key,
                    )
                )

        def _earlier_effect(effect_type: str, **filters: Any) -> ShotNarrativeEffect | None:
            query = select(ShotNarrativeEffect).where(
                ShotNarrativeEffect.project_id == project_id,
                ShotNarrativeEffect.effect_type == effect_type,
            )
            for column, value in filters.items():
                query = query.where(getattr(ShotNarrativeEffect, column) == value)
            for row in session.scalars(query):
                if (row.episode_number, row.scene_sequence, row.shot_sequence) < position:
                    return row
            return None

        for item in directives:
            keyword = item["keyword"]
            if keyword == "ESTABLISH":
                fact_key = item["fact_key"]
                if fact_key in registry["established"]:
                    raise ValueError(f"fact {fact_key!r} is established twice in this script")
                ledger_fact = session.scalar(
                    select(NarrativeFact).where(
                        NarrativeFact.project_id == project_id,
                        NarrativeFact.fact_key == fact_key,
                    )
                )
                if ledger_fact is not None:
                    raise ValueError(
                        f"fact {fact_key!r} is already established on the ledger "
                        f"(episode {ledger_fact.established_episode})"
                    )
                earlier = _earlier_effect("ESTABLISH_FACT", fact_key=fact_key)
                if earlier is not None:
                    raise ValueError(
                        f"fact {fact_key!r} is already declared by an earlier shot "
                        f"(episode {earlier.episode_number})"
                    )
                _declare_effect(
                    effect_type=ShotNarrativeEffectType.ESTABLISH_FACT.value,
                    fact_key=fact_key,
                    summary=item["summary"],
                    disclose_to=["AUDIENCE", *_holder_keys(item.get("holders", []))],
                )
                registry["established"][fact_key] = shot
            elif keyword == "DISCLOSE":
                fact_key = item["fact_key"]
                establishing = registry["established"].get(fact_key)
                if establishing is None:
                    ledger_fact = session.scalar(
                        select(NarrativeFact).where(
                            NarrativeFact.project_id == project_id,
                            NarrativeFact.fact_key == fact_key,
                        )
                    )
                    pending = (
                        _earlier_effect("ESTABLISH_FACT", fact_key=fact_key)
                        if ledger_fact is None
                        else None
                    )
                    if ledger_fact is None and pending is None:
                        raise ValueError(
                            f"cannot DISCLOSE {fact_key!r}: never established at an "
                            "earlier position"
                        )
                    establishing_shot_id = (
                        ledger_fact.established_shot_id if ledger_fact else pending.shot_id  # type: ignore[union-attr]
                    )
                else:
                    establishing_shot_id = establishing.id
                for holder in _holder_keys(item["holders"]):
                    _declare_effect(
                        effect_type=ShotNarrativeEffectType.DISCLOSE_FACT.value,
                        fact_key=fact_key,
                        holder_key=holder,
                        summary=f"discloses {fact_key} to {holder}",
                    )
                if establishing_shot_id != shot.id:
                    _declare_dependency(
                        ShotDependencyType.FACT_REVELATION.value,
                        fact_key=fact_key,
                        summary=f"reveals the established fact {fact_key}",
                    )
            elif keyword == "FORESHADOW":
                obligation_key = item["obligation_key"]
                if obligation_key in registry["opened"]:
                    raise ValueError(
                        f"obligation {obligation_key!r} is opened twice in this script"
                    )
                ledger_obligation = session.scalar(
                    select(NarrativeObligation).where(
                        NarrativeObligation.project_id == project_id,
                        NarrativeObligation.obligation_key == obligation_key,
                    )
                )
                if ledger_obligation is not None:
                    raise ValueError(
                        f"obligation {obligation_key!r} is already opened on the ledger "
                        f"(episode {ledger_obligation.opened_episode})"
                    )
                earlier = _earlier_effect("OPEN_OBLIGATION", obligation_key=obligation_key)
                if earlier is not None:
                    raise ValueError(
                        f"obligation {obligation_key!r} is already declared by an earlier "
                        f"shot (episode {earlier.episode_number})"
                    )
                _declare_effect(
                    effect_type=ShotNarrativeEffectType.OPEN_OBLIGATION.value,
                    obligation_key=obligation_key,
                    summary=item["promise"],
                    metadata_json={"category": "FORESHADOWING"},
                )
                registry["opened"][obligation_key] = shot
            elif keyword == "PAYOFF":
                obligation_key = item["obligation_key"]
                if obligation_key in registry["settled"]:
                    raise ValueError(
                        f"obligation {obligation_key!r} is paid off twice in this script"
                    )
                opening_shot_id: str | None = None
                opening = registry["opened"].get(obligation_key)
                if opening is not None:
                    opening_shot_id = opening.id
                else:
                    ledger_obligation = session.scalar(
                        select(NarrativeObligation).where(
                            NarrativeObligation.project_id == project_id,
                            NarrativeObligation.obligation_key == obligation_key,
                        )
                    )
                    if ledger_obligation is not None:
                        if ledger_obligation.status != "OPEN":
                            raise ValueError(
                                f"cannot PAYOFF {obligation_key!r}: it is already "
                                f"{ledger_obligation.status}"
                            )
                        opening_shot_id = ledger_obligation.opened_shot_id
                    else:
                        pending = _earlier_effect(
                            "OPEN_OBLIGATION", obligation_key=obligation_key
                        )
                        if pending is None:
                            raise ValueError(
                                f"cannot PAYOFF {obligation_key!r}: never opened at an "
                                "earlier position"
                            )
                        opening_shot_id = pending.shot_id
                _declare_effect(
                    effect_type=ShotNarrativeEffectType.SETTLE_OBLIGATION.value,
                    obligation_key=obligation_key,
                    summary=item.get("reason", ""),
                )
                registry["settled"][obligation_key] = shot
                _declare_dependency(
                    ShotDependencyType.OBLIGATION_FULFILLMENT.value,
                    obligation_key=obligation_key,
                    summary=f"pays off the obligation {obligation_key}",
                )
                if opening_shot_id and opening_shot_id != shot.id:
                    _declare_dependency(
                        ShotDependencyType.FORESHADOWING.value,
                        source_shot_id=opening_shot_id,
                        summary=f"pays off foreshadowing set up for {obligation_key}",
                    )
        session.flush()

    def compile_episode(self, episode_id: str) -> CompileResult:
        with self.database.session() as session:
            episode = session.get(Episode, episode_id)
            if not episode:
                raise LookupError("episode not found")
            script_hash = hashlib.sha256(episode.script_source.encode("utf-8")).hexdigest()
            existing = episode.script_structured or {}
            if (
                existing.get("script_hash") == script_hash
                and existing.get("compiler_version") == self.version
            ):
                return CompileResult(
                    episode.id,
                    list(existing.get("scene_ids", [])),
                    list(existing.get("shot_ids", [])),
                    list(existing.get("event_ids", [])),
                    dict(existing.get("entities", {})),
                    script_hash,
                )
            committed = session.scalar(
                select(func.count(Shot.id))
                .join(Scene, Shot.scene_id == Scene.id)
                .where(Scene.episode_id == episode.id, Shot.status == ShotStatus.COMMITTED.value)
            )
            if committed:
                raise RuntimeError("cannot recompile an episode containing committed shots")

            lines = self._lines(episode.script_source)
            raw_groups: list[tuple[str, list[dict[str, Any]]]] = []
            current_header = "Scene 1"
            current_lines: list[dict[str, Any]] = []
            pending_directives: list[dict[str, Any]] = []

            def _drain_trailing() -> None:
                # Directives after a scene's last beat annotate that beat.
                if pending_directives and current_lines:
                    current_lines[-1]["directives"].extend(pending_directives)
                    pending_directives.clear()

            for line in lines:
                directive = self._parse_directive(line)
                if directive is not None:
                    pending_directives.append(directive)
                    continue
                if self._is_scene_header(line):
                    _drain_trailing()
                    if current_lines:
                        raw_groups.append((current_header, current_lines))
                    current_header, current_lines = line, []
                else:
                    for index, source in enumerate(self._split_primary_actions(line)):
                        beat: dict[str, Any] = {"source": source, "directives": []}
                        if index == 0 and pending_directives:
                            beat["directives"] = list(pending_directives)
                            pending_directives.clear()
                        current_lines.append(beat)
            _drain_trailing()
            if pending_directives:
                raise ValueError(
                    "narrative directives with no shot to attach to: "
                    + "; ".join(item["source"] for item in pending_directives)
                )
            if current_lines or not raw_groups:
                current_lines = current_lines or [
                    {"source": "Establish the scene.", "directives": []}
                ]
                raw_groups.append((current_header, current_lines))

            groups: list[dict[str, Any]] = []
            character_names: set[str] = set()
            prop_names: set[str] = set()
            event_sequence = 0
            for scene_sequence, (header, beat_sources) in enumerate(raw_groups, 1):
                location_name, time_context = self._scene_parts(header, scene_sequence)
                parsed_beats: list[dict[str, Any]] = []
                for beat in beat_sources:
                    event_sequence += 1
                    parsed = self._event(beat["source"], event_sequence)
                    parsed["directives"] = beat["directives"]
                    parsed_beats.append(parsed)
                    character_names.update(value for value in (parsed["actor"], parsed["target"]) if value)
                    for item in beat["directives"]:
                        # Directive holders are characters too: a disclosure to
                        # someone who never acts on screen still needs their row.
                        for holder in item.get("holders", []):
                            if holder.upper() != "AUDIENCE":
                                character_names.add(holder)
                    if parsed["object"]:
                        prop_names.add(parsed["object"])
                groups.append(
                    {
                        "header": header,
                        "location": location_name,
                        "time": time_context,
                        "beats": parsed_beats,
                    }
                )

            characters = self._entity_row_map(
                list(session.scalars(select(Character).where(Character.project_id == episode.project_id)))
            )
            for name in sorted(character_names, key=str.casefold):
                if name.casefold() not in characters:
                    character_row = Character(project_id=episode.project_id, name=name, status="DISCOVERED")
                    session.add(character_row)
                    session.flush()
                    characters[name.casefold()] = character_row
            locations = self._entity_row_map(
                list(session.scalars(select(Location).where(Location.project_id == episode.project_id)))
            )
            for name in dict.fromkeys(group["location"] for group in groups):
                if name.casefold() not in locations:
                    location_row = Location(project_id=episode.project_id, name=name)
                    session.add(location_row)
                    session.flush()
                    locations[name.casefold()] = location_row
            props = self._entity_row_map(
                list(session.scalars(select(Prop).where(Prop.project_id == episode.project_id)))
            )
            for name in sorted(prop_names):
                if name.casefold() not in props:
                    prop_row = Prop(project_id=episode.project_id, name=name)
                    session.add(prop_row)
                    session.flush()
                    props[name.casefold()] = prop_row

            # Only derived episode data is replaced. Project-level canonical
            # entity rows are intentionally retained so their UUIDs remain stable.
            session.execute(delete(NarrativeEvent).where(NarrativeEvent.episode_id == episode.id))
            old_scenes = list(session.scalars(select(Scene).where(Scene.episode_id == episode.id)))
            old_scene_ids = [scene.id for scene in old_scenes]
            for scene in old_scenes:
                scene.world_state_id = None
            session.flush()
            if old_scene_ids:
                old_shot_ids = list(
                    session.scalars(select(Shot.id).where(Shot.scene_id.in_(old_scene_ids)))
                )
                if old_shot_ids:
                    # An explicit dependency is a contract. Recompiling this
                    # episode deletes its shots, and a shot elsewhere that
                    # declared one of them as its source would be left owing
                    # material that no longer exists — refused loudly here
                    # rather than discovered as an unresolvable dependency at
                    # generation time.
                    external_dependent = session.scalar(
                        select(ShotDependency.id)
                        .where(
                            ShotDependency.source_shot_id.in_(old_shot_ids),
                            ShotDependency.target_shot_id.not_in(old_shot_ids),
                        )
                        .limit(1)
                    )
                    if external_dependent is not None:
                        raise RuntimeError(
                            "cannot recompile: shots outside this episode declare explicit "
                            "dependencies on its shots; remove those dependencies first"
                        )
                    # A later episode that continues from this one chains its
                    # first shot to this episode's last. Recompiling would
                    # delete the shot that chain points at; refuse with the
                    # reason rather than surfacing a foreign-key violation.
                    external_chain = session.scalar(
                        select(Shot.id)
                        .where(
                            Shot.previous_shot_id.in_(old_shot_ids),
                            Shot.id.not_in(old_shot_ids),
                        )
                        .limit(1)
                    )
                    if external_chain is not None:
                        raise RuntimeError(
                            "cannot recompile: a later episode continues from this episode's "
                            "shots; unlink or recompile that continuation first"
                        )
                    # The mirror direction: this episode is itself a linked
                    # continuation, so an earlier episode's tail points at the
                    # first shot about to be deleted. The continuation service
                    # unlinks before recompiling and re-links afterwards; a
                    # plain recompile must not silently sever that contract.
                    inbound_chain = session.scalar(
                        select(Shot.id)
                        .where(
                            Shot.next_shot_id.in_(old_shot_ids),
                            Shot.id.not_in(old_shot_ids),
                        )
                        .limit(1)
                    )
                    if inbound_chain is not None:
                        raise RuntimeError(
                            "cannot recompile: this episode is a linked continuation of an "
                            "earlier one; confirm it through its episode continuation instead"
                        )
                    session.execute(
                        delete(ShotDependency).where(
                            ShotDependency.target_shot_id.in_(old_shot_ids)
                        )
                    )
                    # Transitions are derived rows and must go before their
                    # shots. Relying on the target FK's cascade is not enough
                    # on PostgreSQL: the source FK's NO ACTION check trigger
                    # was created first, so it fires before the cascade removes
                    # the row and refuses the whole delete — a latent defect
                    # this was the first PostgreSQL recompile-with-transitions
                    # to reach.
                    session.execute(
                        delete(TimelineTransition).where(
                            TimelineTransition.target_shot_id.in_(old_shot_ids)
                            | TimelineTransition.source_shot_id.in_(old_shot_ids)
                        )
                    )
                session.execute(delete(Shot).where(Shot.scene_id.in_(old_scene_ids)))
            session.execute(delete(TimelineState).where(TimelineState.episode_id == episode.id))
            if old_scene_ids:
                session.execute(delete(Scene).where(Scene.id.in_(old_scene_ids)))
            session.flush()

            entity_nodes: list[dict[str, Any]] = []
            character_keys = {name.casefold() for name in character_names}
            prop_keys = {name.casefold() for name in prop_names}
            for row in sorted(characters.values(), key=lambda item: item.name.casefold()):
                if row.name.casefold() in character_keys:
                    entity_nodes.append({"id": row.id, "type": "CHARACTER", "name": row.name})
            for group in groups:
                row = locations[group["location"].casefold()]
                if not any(node["id"] == row.id for node in entity_nodes):
                    entity_nodes.append({"id": row.id, "type": "LOCATION", "name": row.name})
            for row in sorted(props.values(), key=lambda item: item.name.casefold()):
                if row.name.casefold() in prop_keys:
                    entity_nodes.append({"id": row.id, "type": "PROP", "name": row.name})

            graph_edges: list[dict[str, Any]] = []
            event_timeline: list[dict[str, Any]] = []
            state_transitions: list[dict[str, Any]] = []
            scene_ids: list[str] = []
            shot_ids: list[str] = []
            event_ids: list[str] = []
            previous_shot: Shot | None = None
            previous_output: TimelineState | None = None
            # What this compile has declared so far, so directives can
            # reference material from earlier shots of the same script and
            # duplicates fail at compile time rather than at commit.
            directive_registry: dict[str, dict[str, Shot]] = {
                "established": {},
                "opened": {},
                "settled": {},
            }
            for scene_sequence, group in enumerate(groups, 1):
                location = locations[group["location"].casefold()]
                scene = Scene(
                    episode_id=episode.id,
                    location_id=location.id,
                    sequence=scene_sequence,
                    description=location.name,
                    scene_description=location.name,
                    time_context=group["time"],
                )
                session.add(scene)
                session.flush()
                scene_ids.append(scene.id)
                base_state = self._base_state(location, group["time"])
                scene_state = TimelineState(
                    project_id=episode.project_id,
                    episode_id=episode.id,
                    scene_id=scene.id,
                    previous_state_id=previous_output.id if previous_output else None,
                    state_kind="SCENE_START",
                    state_json=deepcopy(base_state),
                )
                session.add(scene_state)
                session.flush()
                scene.world_state_id = scene_state.id
                active_state = deepcopy(base_state)
                prior_in_scene: TimelineState | None = None
                for shot_sequence, parsed in enumerate(group["beats"], 1):
                    actor = characters.get(str(parsed["actor"]).casefold()) if parsed["actor"] else None
                    target = characters.get(str(parsed["target"]).casefold()) if parsed["target"] else None
                    prop = props.get(str(parsed["object"]).casefold()) if parsed["object"] else None
                    event_scope = f"{episode.id}:{parsed['sequence']}:{parsed['source']}"
                    event_id = self._stable_id(episode.project_id, "event", event_scope)
                    action_entity_id = self._stable_id(episode.project_id, "action", event_scope)
                    fact_entity_id = self._stable_id(episode.project_id, "narrative_fact", event_scope)
                    entity_nodes.extend(
                        [
                            {
                                "id": action_entity_id,
                                "type": "ACTION",
                                "name": parsed["action"],
                                "source": parsed["source"],
                            },
                            {
                                "id": fact_entity_id,
                                "type": "NARRATIVE_FACT",
                                "name": parsed["source"],
                            },
                        ]
                    )
                    if parsed["dialogue"]:
                        entity_nodes.append(
                            {
                                "id": self._stable_id(episode.project_id, "dialogue", event_scope),
                                "type": "DIALOGUE",
                                "name": parsed["dialogue"],
                            }
                        )
                    for relation, source_id, target_id in (
                        ("PERFORMS", actor.id if actor else None, action_entity_id),
                        ("TARGETS", action_entity_id, target.id if target else None),
                        ("USES", action_entity_id, prop.id if prop else None),
                        ("ESTABLISHES", action_entity_id, fact_entity_id),
                    ):
                        if source_id and target_id:
                            relation_scope = f"{event_scope}:{relation}:{source_id}:{target_id}"
                            relation_id = self._stable_id(episode.project_id, "relationship", relation_scope)
                            entity_nodes.append(
                                {
                                    "id": relation_id,
                                    "type": "RELATIONSHIP",
                                    "name": relation,
                                }
                            )
                            graph_edges.append(
                                {
                                    "id": relation_id,
                                    "type": relation,
                                    "source_id": source_id,
                                    "target_id": target_id,
                                    "event_id": event_id,
                                }
                            )

                    pre_state = deepcopy(active_state)
                    input_state = TimelineState(
                        project_id=episode.project_id,
                        episode_id=episode.id,
                        scene_id=scene.id,
                        previous_state_id=(prior_in_scene.id if prior_in_scene else scene_state.id),
                        state_kind="SHOT_INPUT",
                        state_json=deepcopy(pre_state),
                    )
                    session.add(input_state)
                    session.flush()
                    post_state, effects = self._apply_event(
                        pre_state,
                        parsed,
                        event_id=event_id,
                        actor_id=actor.id if actor else None,
                        target_id=target.id if target else None,
                        object_id=prop.id if prop else None,
                    )
                    output_state = TimelineState(
                        project_id=episode.project_id,
                        episode_id=episode.id,
                        scene_id=scene.id,
                        previous_state_id=input_state.id,
                        state_kind="SHOT_OUTPUT",
                        state_json=deepcopy(post_state),
                    )
                    session.add(output_state)
                    event = NarrativeEvent(
                        id=event_id,
                        episode_id=episode.id,
                        actor_id=actor.id if actor else None,
                        action=parsed["action"],
                        target_id=target.id if target else None,
                        object_id=prop.id if prop else None,
                        dialogue=parsed["dialogue"],
                        preconditions={"state": pre_state},
                        effects=effects,
                        sequence=parsed["sequence"],
                        timeline_position=float(parsed["sequence"]),
                        source_text=parsed["source"],
                    )
                    session.add(event)
                    session.flush()
                    shot = Shot(
                        scene_id=scene.id,
                        sequence=shot_sequence,
                        shot_type="DIALOGUE" if parsed["dialogue"] else "ACTION",
                        duration=6.0 if parsed["dialogue"] else 5.0,
                        user_prompt=parsed["source"],
                        compiled_prompt=parsed["source"],
                        prompt=parsed["source"],
                        previous_shot_id=previous_shot.id if previous_shot else None,
                        input_state_id=input_state.id,
                        output_state_id=output_state.id,
                        status=ShotStatus.PLANNED.value,
                    )
                    session.add(shot)
                    session.flush()
                    input_state.shot_id = shot.id
                    output_state.shot_id = shot.id
                    if previous_shot:
                        previous_shot.next_shot_id = shot.id
                        transition_type = (
                            TimelineTransitionType.CONTINUOUS
                            if previous_shot.scene_id == shot.scene_id
                            else TimelineTransitionType.SCENE_CUT
                        )
                        if transition_type is TimelineTransitionType.CONTINUOUS:
                            # Structural, not semantic: a continuous shot
                            # explicitly inherits the previous shot's committed
                            # state. Foreshadowing, revelation and obligation
                            # dependencies carry story meaning a rules compiler
                            # cannot derive; they are declared through manual
                            # editing instead.
                            session.add(
                                ShotDependency(
                                    project_id=episode.project_id,
                                    target_shot_id=shot.id,
                                    source_shot_id=previous_shot.id,
                                    dependency_type=ShotDependencyType.STATE_INHERITANCE.value,
                                    summary=(
                                        "inherits in-scene state from the previous shot: "
                                        + (previous_shot.user_prompt or previous_shot.prompt)
                                    ),
                                    origin=ShotDependencyOrigin.SCRIPT_COMPILER.value,
                                    dependency_key=ShotDependency.natural_key(
                                        ShotDependencyType.STATE_INHERITANCE.value,
                                        source_shot_id=previous_shot.id,
                                    ),
                                )
                            )
                        session.add(
                            TimelineTransition(
                                project_id=episode.project_id,
                                source_shot_id=previous_shot.id,
                                target_shot_id=shot.id,
                                transition_type=transition_type.value,
                                reconciliation_required=False,
                                metadata_json={
                                    "source": "narrative_compiler",
                                    "propagation_semantics": (
                                        "FULL"
                                        if transition_type is TimelineTransitionType.CONTINUOUS
                                        else "RESET_BOUNDARY"
                                    ),
                                    "spatial_state": (
                                        "PROPAGATE"
                                        if transition_type is TimelineTransitionType.CONTINUOUS
                                        else "RESET"
                                    ),
                                    "character_state": (
                                        "PROPAGATE"
                                        if transition_type is TimelineTransitionType.CONTINUOUS
                                        else "MAY_PROPAGATE_WITH_EXPLICIT_OPT_IN"
                                    ),
                                    "propagate_character_state": False,
                                    "inferred_from": (
                                        "narrative_sequence"
                                        if transition_type is TimelineTransitionType.CONTINUOUS
                                        else "scene_boundary"
                                    ),
                                },
                            )
                        )
                    if parsed.get("directives"):
                        self._apply_shot_directives(
                            session,
                            episode=episode,
                            shot=shot,
                            scene_sequence=scene_sequence,
                            shot_sequence=shot_sequence,
                            directives=parsed["directives"],
                            characters=characters,
                            registry=directive_registry,
                        )
                    shot_ids.append(shot.id)
                    event_ids.append(event.id)
                    event_timeline.append(
                        {
                            "id": event.id,
                            "sequence": event.sequence,
                            "scene_id": scene.id,
                            "shot_id": shot.id,
                            "pre_state": pre_state,
                            "actor": event.actor_id,
                            "action": event.action,
                            "target": event.target_id,
                            "object": event.object_id,
                            "dialogue": event.dialogue,
                            "effects": effects,
                            "post_state": post_state,
                            "primary_action": parsed["action"],
                            "minor_reaction": parsed["minor_reaction"],
                        }
                    )
                    state_transitions.append(
                        {
                            "event_id": event.id,
                            "shot_id": shot.id,
                            "from_state_id": input_state.id,
                            "to_state_id": output_state.id,
                            "primary_action": parsed["action"],
                            "effects": effects,
                        }
                    )
                    active_state = post_state
                    prior_in_scene = output_state
                    previous_shot = shot
                    previous_output = output_state

            entities = {
                "characters": sorted(character_names, key=str.casefold),
                "locations": [group["location"] for group in groups],
                "props": sorted(prop_names),
            }
            episode.script_structured = {
                "compiler_version": self.version,
                "script_hash": script_hash,
                "scene_ids": scene_ids,
                "shot_ids": shot_ids,
                "event_ids": event_ids,
                "entities": entities,
                "entity_graph": {"nodes": entity_nodes, "edges": graph_edges},
                "event_timeline": event_timeline,
                "state_transition_graph": state_transitions,
                "shot_rule": "ONE_PRIMARY_VISUAL_ACTION_WITH_OPTIONAL_MINOR_REACTION",
                "authoritative_state_source": "SQL_TIMELINE_STATE",
            }
            episode.status = "COMPILED"
            session.flush()
            return CompileResult(episode.id, scene_ids, shot_ids, event_ids, entities, script_hash)
