from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from platform_contracts import CanonicalShotSpec
from pydantic import BaseModel, Field


class AdapterInput(BaseModel):
    shot: CanonicalShotSpec
    context: dict[str, Any] = Field(default_factory=dict)


class ModelGenerationRequest(BaseModel):
    provider: str
    model: str
    prompt: str
    negative_prompt: str = ""
    payload: dict[str, Any]
    asset_bindings: list[str] = Field(default_factory=list)
    continuity_assertions: list[str] = Field(default_factory=list)


class VideoModelAdapter(ABC):
    name: str

    @abstractmethod
    def compile(self, model: str, value: AdapterInput) -> ModelGenerationRequest: ...


def canonical_lines(spec: CanonicalShotSpec, context: dict[str, Any]) -> list[str]:
    shot = spec.model_dump(mode="json")
    subjects = shot.get("subjects") or []
    subject_line = "; ".join(
        f"{subject.get('name', subject.get('asset_version_id', 'subject'))}: "
        f"{subject.get('screen_position', 'position fixed')}, "
        f"body {subject.get('body_orientation', 'orientation fixed')}, "
        f"eyes toward {subject.get('eyeline_target', 'the approved scene target')}"
        for subject in subjects
    )
    camera = shot.get("camera") or {}
    lighting = shot.get("lighting") or {}
    lines = [
        f"Shot intent: {shot.get('intent', '')}",
        f"Subjects: {subject_line or 'preserve approved subjects and identities'}",
        f"Start state: {shot.get('start_state', {})}",
        f"Single action: {shot.get('dominant_action', '')}",
        f"Blocking: {shot.get('blocking', {})}",
        "Camera: "
        f"position={camera.get('position', 'approved')}; "
        f"movement={camera.get('dominant_movement', 'locked')}; "
        f"speed={camera.get('speed', 'steady')}; path={camera.get('path', 'none')}; "
        f"framing={camera.get('framing', 'approved')}; focus={camera.get('focus', 'subject')}",
        f"Lighting: {lighting}",
        f"Dialogue: {shot.get('dialogue', '')}",
        f"Audio: {shot.get('audio', {})}",
        f"End state: {shot.get('end_state', {})}",
        f"Continuity: {shot.get('continuity', {})}",
        f"Locked visual style: {shot.get('style_lock', {})}",
        f"Canonical context assets: {context.get('canonical_asset_ids', [])}",
        f"Previous final frame: {context.get('previous_final_frame_asset_id', '')}",
        f"Constraints: {shot.get('constraints', [])}",
    ]
    assembled_context = str(context.get("assembled_text") or "").strip()
    if assembled_context:
        lines.insert(-1, f"Bounded production context:\n{assembled_context}")
    return lines


def common_payload(spec: CanonicalShotSpec, context: dict[str, Any]) -> dict[str, Any]:
    shot = spec.model_dump(mode="json")
    references = list(dict.fromkeys(context.get("reference_images", [])))
    reference_videos = list(dict.fromkeys(context.get("reference_videos", [])))
    return {
        "duration": shot.get("duration", 8),
        "resolution": shot.get("resolution", "720p"),
        "aspect_ratio": shot.get("aspect_ratio", "9:16"),
        "reference_images": references,
        "start_frame": context.get("start_frame") or context.get("previous_final_frame_asset_id"),
        "end_frame": context.get("end_frame"),
        "reference_video": context.get("reference_video")
        or (reference_videos[0] if reference_videos else None),
        # Footage the shot continues from, as distinct from footage it merely
        # references. A provider that conflates the two continues from a clip it
        # was only meant to take style from.
        "first_clip": context.get("first_clip") or context.get("previous_clip"),
        "audio": shot.get("audio") or {},
        "style_embedding": (context.get("style_control") or {}).get("embedding"),
        "style_control": context.get("style_control"),
    }
