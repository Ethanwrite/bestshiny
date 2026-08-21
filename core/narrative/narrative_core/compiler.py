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
    Prop,
    Scene,
    Shot,
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

    version = "narrative-rules-v2"

    def __init__(self, database: Database):
        self.database = database

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
            raw_groups: list[tuple[str, list[str]]] = []
            current_header = "Scene 1"
            current_lines: list[str] = []
            for line in lines:
                if self._is_scene_header(line):
                    if current_lines:
                        raw_groups.append((current_header, current_lines))
                    current_header, current_lines = line, []
                else:
                    current_lines.extend(self._split_primary_actions(line))
            if current_lines or not raw_groups:
                raw_groups.append((current_header, current_lines or ["Establish the scene."]))

            groups: list[dict[str, Any]] = []
            character_names: set[str] = set()
            prop_names: set[str] = set()
            event_sequence = 0
            for scene_sequence, (header, beat_sources) in enumerate(raw_groups, 1):
                location_name, time_context = self._scene_parts(header, scene_sequence)
                parsed_beats: list[dict[str, Any]] = []
                for source in beat_sources:
                    event_sequence += 1
                    parsed = self._event(source, event_sequence)
                    parsed_beats.append(parsed)
                    character_names.update(value for value in (parsed["actor"], parsed["target"]) if value)
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
