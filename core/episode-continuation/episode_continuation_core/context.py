"""EpisodeContinuationContext: what the next episode inherits, explicitly.

The snapshot is assembled entirely from systems that already exist - the
narrative ledger (facts, disclosures, open obligations), the persistent
character state heads, the authoritative timeline states, the project style
lock and the visual bible. Nothing here re-derives story truth; it collects
the heads and stamps, per continuity class, whether the next episode inherits
or resets. That per-class verdict is the contract the linker and the frame
anchor planner then enforce:

    narrative   facts / obligations / disclosures      always inherited
    character   state heads, identity versions          always inherited
    scene       location, spatial state, lighting       CONTINUOUS only
    visual      style lock, visual bible                always inherited
    frame       the previous episode's tail frame       CONTINUOUS only
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from narrative_ledger_core import NarrativeLedgerService
from platform_database import Database
from production_domain.models import (
    Character,
    CharacterStateHead,
    CharacterStateVersion,
    Episode,
    Location,
    ProjectStyleLock,
    Scene,
    Shot,
    ShotStatus,
    TimelineState,
    VisualBibleVersion,
)
from sqlalchemy import select

CONTEXT_VERSION = "episode-continuation-v1"

#: Per-mode inheritance verdicts, one per continuity class.
CONTINUITY_CLASSES: dict[str, dict[str, str]] = {
    "CONTINUOUS": {
        "narrative": "INHERIT",
        "character": "INHERIT",
        "scene": "INHERIT",
        "visual": "INHERIT",
        "frame": "INHERIT",
    },
    "TIME_JUMP": {
        "narrative": "INHERIT",
        "character": "INHERIT",
        "scene": "RESET",
        "visual": "INHERIT",
        "frame": "RESET",
    },
    "LOCATION_CHANGE": {
        "narrative": "INHERIT",
        "character": "INHERIT",
        "scene": "RESET",
        "visual": "INHERIT",
        "frame": "RESET",
    },
}


def context_hash(context: dict[str, Any]) -> str:
    encoded = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ContinuationContextBuilder:
    version = CONTEXT_VERSION

    def __init__(self, database: Database, ledger: NarrativeLedgerService):
        self.database = database
        self.ledger = ledger

    def build(
        self, project_id: str, *, previous_episode_id: str, continuation_mode: str
    ) -> dict[str, Any]:
        with self.database.session() as session:
            episode = session.get(Episode, previous_episode_id)
            if episode is None or episode.project_id != project_id:
                raise LookupError("previous episode not found in this project")

            last_shot: Shot | None = None
            last_scene: Scene | None = None
            for scene in session.scalars(
                select(Scene)
                .where(Scene.episode_id == episode.id)
                .order_by(Scene.sequence.desc())
            ):
                shot = session.scalar(
                    select(Shot)
                    .where(Shot.scene_id == scene.id)
                    .order_by(Shot.sequence.desc())
                    .limit(1)
                )
                if shot is not None:
                    last_shot, last_scene = shot, scene
                    break
            if last_shot is None or last_scene is None:
                raise LookupError(
                    "previous episode has no shots; compile it before continuing the series"
                )

            output_state = (
                session.get(TimelineState, last_shot.output_state_id)
                if last_shot.output_state_id
                else None
            )
            ending_state = dict(output_state.state_json) if output_state is not None else {}
            location = (
                session.get(Location, last_scene.location_id) if last_scene.location_id else None
            )

            all_committed = True
            shot_count = 0
            for scene in session.scalars(select(Scene).where(Scene.episode_id == episode.id)):
                for shot in session.scalars(select(Shot).where(Shot.scene_id == scene.id)):
                    shot_count += 1
                    if shot.status != ShotStatus.COMMITTED.value:
                        all_committed = False

            characters = self._character_states(session, project_id, ending_state)
            style_lock = session.scalar(
                select(ProjectStyleLock).where(ProjectStyleLock.project_id == project_id)
            )
            bible = session.scalar(
                select(VisualBibleVersion)
                .where(
                    VisualBibleVersion.project_id == project_id,
                    VisualBibleVersion.status == "LOCKED",
                )
                .order_by(VisualBibleVersion.version.desc())
            )
            next_number = episode.episode_number + 1

        series = self.ledger.series_context(project_id, episode=next_number)
        context: dict[str, Any] = {
            "version": self.version,
            "previous_episode": {
                "id": episode.id,
                "number": episode.episode_number,
                "title": episode.title,
                "status": episode.status,
                "shot_count": shot_count,
                "all_shots_committed": all_committed,
            },
            "ending": {
                "shot_id": last_shot.id,
                "shot_status": last_shot.status,
                "scene_id": last_scene.id,
                "location": {
                    "id": location.id if location else None,
                    "name": location.name if location else last_scene.description,
                },
                "time_context": last_scene.time_context,
                "output_state": ending_state,
                "end_frame_asset_id": last_shot.end_frame_asset_id,
            },
            "characters": characters,
            "narrative": {
                "known_facts": dict(series.known_facts),
                "audience_only_facts": list(series.audience_only_facts),
                "open_obligations": list(series.open_obligations),
            },
            "props": {
                "in_scene": ending_state.get("props", {}),
                "held": ending_state.get("held_props", {}),
                "costume": ending_state.get("costume", {}),
            },
            "style_lock": {
                "locked": style_lock is not None,
                "style_version_id": style_lock.style_version_id if style_lock is not None else None,
            },
            "visual_bible": (
                {"id": bible.id, "version": bible.version, "locked": True}
                if bible is not None
                else None
            ),
            "continuity_classes": CONTINUITY_CLASSES[continuation_mode],
        }
        return context

    @staticmethod
    def _character_states(
        session: Any, project_id: str, ending_state: dict[str, Any]
    ) -> list[dict[str, Any]]:
        rows = list(session.scalars(select(Character).where(Character.project_id == project_id)))
        state_characters: dict[str, Any] = ending_state.get("characters", {}) or {}
        views: list[dict[str, Any]] = []
        for character in rows:
            heads = list(
                session.scalars(
                    select(CharacterStateHead).where(
                        CharacterStateHead.project_id == project_id,
                        CharacterStateHead.character_id == character.id,
                    )
                )
            )
            head_views = []
            for head in heads:
                version = session.get(CharacterStateVersion, head.state_version_id)
                head_views.append(
                    {
                        "scope": head.timeline_scope_key,
                        "state_version_id": head.state_version_id,
                        "version": version.version if version is not None else None,
                        "state_hash": version.state_hash if version is not None else None,
                    }
                )
            in_ending = character.id in state_characters
            views.append(
                {
                    "character_id": character.id,
                    "name": character.name,
                    "identity_version_id": character.current_identity_version_id,
                    "state_heads": head_views,
                    "present_in_ending": in_ending,
                    "ending_view": state_characters.get(character.id),
                }
            )
        return views
