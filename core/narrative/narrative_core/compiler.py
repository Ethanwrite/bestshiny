from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from platform_database import Database
from production_domain.models import (
    Character,
    Episode,
    NarrativeEvent,
    Scene,
    Shot,
    ShotStatus,
    TimelineState,
)
from sqlalchemy import delete, func, select

SCENE_HEADER = re.compile(
    r"^(?:(INT\.?|EXT\.?|内景|外景)\s*)?(.+?)(?:\s*[-—]\s*(DAY|NIGHT|日|夜|黄昏|清晨|雨夜))?$",
    re.IGNORECASE,
)
DIALOGUE = re.compile(r"^([\w\u4e00-\u9fff][\w\u4e00-\u9fff ·]{0,23})[:：]\s*(.+)$")
SCENE_PREFIXES = ("INT.", "EXT.", "INT ", "EXT ", "内景", "外景", "场景")


@dataclass(frozen=True)
class CompileResult:
    episode_id: str
    scene_ids: list[str]
    shot_ids: list[str]
    event_ids: list[str]
    entities: dict[str, list[str]]
    script_hash: str


class NarrativeCompiler:
    """Deterministic V1 compiler: script lines become events, visual beats, shots and state transitions."""

    version = "narrative-rules-v1"

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
    def _event(line: str, sequence: int) -> dict[str, Any]:
        dialogue = DIALOGUE.match(line)
        if dialogue:
            actor, words = dialogue.groups()
            return {
                "actor": actor.strip(),
                "action": "speak",
                "dialogue": words.strip(),
                "source": line,
                "effects": {f"{actor.strip()}.last_action": "speaking"},
                "sequence": sequence,
            }
        actor = None
        latin = re.match(r"^([A-Z][A-Za-z0-9_-]{1,30})\s+", line)
        chinese = re.match(r"^([\u4e00-\u9fff]{2,4})(?=抬|走|转|看|拿|放|推|拉|站|坐|说|跑|停)", line)
        if latin:
            actor = latin.group(1)
        elif chinese:
            actor = chinese.group(1)
        effects = {"narrative.last_action": line}
        if actor:
            effects[f"{actor}.last_action"] = line
        return {
            "actor": actor,
            "action": line,
            "dialogue": "",
            "source": line,
            "effects": effects,
            "sequence": sequence,
        }

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

            lines = self._lines(episode.script_source)
            groups: list[tuple[str, list[str]]] = []
            current_header = "Scene 1"
            current_lines: list[str] = []
            for line in lines:
                if self._is_scene_header(line):
                    if current_lines:
                        groups.append((current_header, current_lines))
                    current_header, current_lines = line, []
                else:
                    current_lines.append(line)
            if current_lines or not groups:
                groups.append((current_header, current_lines or ["Establish the scene."]))

            scene_ids: list[str] = []
            shot_ids: list[str] = []
            event_ids: list[str] = []
            characters: set[str] = set()
            previous_shot: Shot | None = None
            previous_output: TimelineState | None = None
            event_sequence = 0
            for scene_sequence, (header, beats) in enumerate(groups, 1):
                location, time_context = self._scene_parts(header, scene_sequence)
                scene = Scene(
                    episode_id=episode.id,
                    sequence=scene_sequence,
                    description=location,
                    scene_description=location,
                    time_context=time_context,
                )
                session.add(scene)
                session.flush()
                scene_ids.append(scene.id)
                base_state: dict[str, Any] = {
                    "scene": {"location": location, "time": time_context},
                    "characters": {},
                    "camera": {"axis": "A", "shot_size": "medium"},
                    "important_facts": [],
                }
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
                active_state: dict[str, Any] = deepcopy(base_state)
                for shot_sequence, beat in enumerate(beats, 1):
                    event_sequence += 1
                    parsed = self._event(beat, event_sequence)
                    if parsed["actor"]:
                        characters.add(parsed["actor"])
                        active_state["characters"].setdefault(
                            parsed["actor"],
                            {"position": "midground_center", "orientation": "scene_partner", "visible": True},
                        )
                    input_state = TimelineState(
                        project_id=episode.project_id,
                        episode_id=episode.id,
                        scene_id=scene.id,
                        previous_state_id=previous_output.id if previous_output else scene_state.id,
                        state_kind="SHOT_INPUT",
                        state_json=deepcopy(active_state),
                    )
                    session.add(input_state)
                    session.flush()
                    active_state.update({"last_event": parsed["action"]})
                    active_state["important_facts"] = list(active_state.get("important_facts", [])) + [
                        parsed["source"]
                    ]
                    output_state = TimelineState(
                        project_id=episode.project_id,
                        episode_id=episode.id,
                        scene_id=scene.id,
                        previous_state_id=input_state.id,
                        state_kind="SHOT_OUTPUT",
                        state_json=deepcopy(active_state),
                    )
                    session.add(output_state)
                    session.flush()
                    event = NarrativeEvent(
                        episode_id=episode.id,
                        actor_id=parsed["actor"],
                        action=parsed["action"],
                        dialogue=parsed["dialogue"],
                        effects=parsed["effects"],
                        sequence=event_sequence,
                        timeline_position=float(event_sequence),
                        source_text=parsed["source"],
                    )
                    session.add(event)
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
                    scene_ids[-1] = scene.id
                    shot_ids.append(shot.id)
                    event_ids.append(event.id)
                    previous_shot = shot
                    previous_output = output_state

            existing_names = set(
                session.scalars(select(Character.name).where(Character.project_id == episode.project_id))
            )
            for name in sorted(characters - existing_names):
                session.add(Character(project_id=episode.project_id, name=name, status="DISCOVERED"))
            entities = {"characters": sorted(characters), "locations": [group[0] for group in groups]}
            episode.script_structured = {
                "compiler_version": self.version,
                "script_hash": script_hash,
                "scene_ids": scene_ids,
                "shot_ids": shot_ids,
                "event_ids": event_ids,
                "entities": entities,
            }
            episode.status = "COMPILED"
            session.flush()
            return CompileResult(episode.id, scene_ids, shot_ids, event_ids, entities, script_hash)
