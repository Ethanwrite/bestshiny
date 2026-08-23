"""Offline end-to-end simulation: shot cues, style lock and the narrative ledger.

Deliberately calls no video model. It exercises the deterministic compile path
and the series ledger on a three-shot story, then prints what a renderer would
actually receive so the prompt package and the style lock can be inspected.

    uv run python scripts/simulate_short_story.py
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

# Importing the API package constructs its module-level app, which would
# otherwise bind to the developer database. Keep this simulation self-contained.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEPLOYMENT_ENVIRONMENT"] = "test"
os.environ["PROVIDER_MODE"] = "mock"
os.environ["ALLOW_LIVE_PROVIDER_CALLS"] = "false"

from narrative_ledger_core import AUDIENCE, KnowledgeViolation, NarrativeLedgerService
from PIL import Image, ImageDraw
from platform_shared import Settings
from production_domain.models import Episode, Project, Scene, Shot, TimelineState, User
from video_platform_api.container import build_container

STORY = "The Letter"


def _style_png() -> bytes:
    image = Image.new("RGB", (96, 96), (38, 74, 88))
    draw = ImageDraw.Draw(image)
    for offset in range(0, 96, 8):
        draw.rectangle((offset, 0, offset + 2, 95), fill=(228, 236, 238))
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    return payload.getvalue()


def _rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    container = build_container(
        Settings(
            _env_file=None,
            database_url="sqlite://",
            storage_root=root / "data" / "simulation-media",
            public_base_url="https://media.invalid",
            deployment_environment="test",
            auth_required=False,
            provider_mode="mock",
        )
    )
    database = container.database
    from production_domain.models import Base

    Base.metadata.create_all(database.engine)

    with database.session() as session:
        actor = User(email="sim@example.com", display_name="Simulation Owner")
        project = Project(title=STORY, default_language="zh-CN")
        session.add_all([actor, project])
        session.flush()
        actor_id, project_id = actor.id, project.id
        episode = Episode(project_id=project_id, title="Episode 1", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="A rainy platform at night")
        session.add(scene)
        session.flush()
        shot_ids: list[str] = []
        beats = [
            (
                "Lin reads the letter once and lowers it.",
                {
                    "characters": {
                        "lin": {
                            "screen_position": "left of centre",
                            "eyeline_target": "the letter in her hands",
                        }
                    }
                },
            ),
            (
                "Lin folds the letter and puts it in her coat.",
                {"characters": {"lin": {"screen_position": "centre", "eyeline_target": "the platform edge"}}},
            ),
            (
                "Mira steps into frame behind Lin and stops.",
                {
                    "characters": {
                        "lin": {"screen_position": "centre", "eyeline_target": "the platform edge"},
                        "mira": {
                            "screen_position": "right, background",
                            "eyeline_target": "the back of Lin's head",
                        },
                    }
                },
            ),
        ]
        for index, (prompt, state) in enumerate(beats, start=1):
            start = TimelineState(
                project_id=project_id,
                episode_id=episode.id,
                scene_id=scene.id,
                state_kind="SHOT_INPUT",
                state_json=state,
            )
            end = TimelineState(
                project_id=project_id,
                episode_id=episode.id,
                scene_id=scene.id,
                state_kind="SHOT_OUTPUT",
                state_json=state,
            )
            session.add_all([start, end])
            session.flush()
            shot = Shot(
                scene_id=scene.id,
                sequence=index,
                prompt=prompt,
                user_prompt=prompt,
                duration=5,
                input_state_id=start.id,
                output_state_id=end.id,
            )
            session.add(shot)
            session.flush()
            start.shot_id = end.shot_id = shot.id
            shot_ids.append(shot.id)

    # --- project style lock -------------------------------------------------
    _rule("1. PROJECT STYLE LOCK")
    media = container.media.register(
        project_id, "REFERENCE", io.BytesIO(_style_png()), filename="style.png", mime_type="image/png"
    )[0]
    asset = container.asset_registry.create(
        project_id,
        "STYLE",
        "Locked look",
        canonical_metadata={
            "constraints": ["muted cyan shadows", "matte illustrated edge treatment"],
            "world_rules": ["every scene uses the locked illustrated rendering language"],
        },
    )
    version = container.asset_registry.add_version(asset.id, primary_media_asset_id=media.id)
    container.asset_registry.promote(asset.id, version.id, reason="approved look")
    lock = container.styles.lock(
        project_id,
        version.id,
        locked_by_user_id=actor_id,
        reason="whole series uses this look",
        explicit_confirmation=True,
    )
    control = container.styles.generation_control(project_id)
    print(f"  locked style version : {lock.style_version_id}")
    print(f"  immutable embedding  : {lock.style_embedding_id}")
    print(f"  similarity threshold : {lock.similarity_threshold}  drift limit: {lock.drift_limit}")
    print(f"  prompt view          : {json.dumps(control.prompt_view(), ensure_ascii=False)[:200]}")
    print(f"  reference media ids  : {list(control.reference_media_ids)}")

    # --- narrative ledger ---------------------------------------------------
    _rule("2. NARRATIVE LEDGER (series memory)")
    ledger = NarrativeLedgerService(database)
    ledger.establish_fact(
        project_id,
        fact_key="letter_is_forged",
        summary="The letter is a forgery.",
        episode=1,
        shot_id=shot_ids[0],
        subject_character_ids=["lin"],
    )
    ledger.open_obligation(
        project_id,
        obligation_key="who_forged_it",
        promise="Who forged the letter must be answered.",
        episode=1,
        shot_id=shot_ids[0],
    )
    ledger.disclose(
        project_id, fact_key="letter_is_forged", holder_key="mira", episode=1, shot_id=shot_ids[2]
    )
    context = ledger.series_context(project_id, episode=1, holder_keys=["lin", "mira"])
    print(f"  audience knows       : {context.known_facts.get(AUDIENCE, [])}")
    print(f"  mira knows           : {context.known_facts.get('mira', [])}")
    print(f"  lin knows            : {context.known_facts.get('lin', [])}   <- dramatic irony")
    print(f"  audience-only facts  : {context.audience_only_facts}")
    print(f"  open obligations     : {context.open_obligations}")
    try:
        ledger.assert_may_act_on(project_id, holder_key="lin", fact_keys=["letter_is_forged"], episode=1)
        print("  lin acting on it     : ALLOWED  <-- WRONG, gate failed")
        return 1
    except KnowledgeViolation as exc:
        print(f"  lin acting on it     : BLOCKED  ({exc})")

    # --- compiled shot cues -------------------------------------------------
    _rule("3. COMPILED SHOT CUES (deterministic backend, no model call)")
    canonical_assets, _ = container.visual_runtime._canonical_assets(project_id)
    # No style-lock injection here on purpose. This script used to mirror what
    # prepare_autopilot did, because the compiler trusted a caller-supplied
    # `style_lock` key and every other caller silently lost the lock. The
    # compiler now resolves the project lock itself, so this plain call is the
    # proof: if the lock stops reaching the spec, this script fails.
    for index, shot_id in enumerate(shot_ids, start=1):
        result = container.prompts.compile(shot_id, canonical_assets=canonical_assets)
        spec = result.spec
        output = result.output
        print(f"\n  --- shot {index} -------------------------------------------------")
        print(f"  status            : {output.status}")
        print(f"  dominant_action   : {spec.dominant_action}")
        print(f"  camera movement   : {spec.camera.dominant_movement}   framing: {spec.camera.framing}")
        print(f"  allow_camera_gaze : {spec.allow_camera_gaze}")
        print(f"  subjects          : {[(s.name, s.eyeline_target) for s in spec.subjects]}")
        print(f"  style_lock present: {bool(spec.style_lock)}  keys={sorted(spec.style_lock)[:4]}")
        print(f"  qc_checklist      : {output.qc_checklist[:4]}")
        print(f"  negative_prompt   : {(output.negative_prompt or '')[:96]}...")
        print(f"  skill version     : {result.skill_version}")
        style_constraint = [c for c in spec.constraints if "locked visual style" in c]
        print(f"  style constraint  : {'PRESENT' if style_constraint else 'MISSING'}")
        if not style_constraint:
            return 1
    print("\n  All shots compiled with the locked style enforced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
