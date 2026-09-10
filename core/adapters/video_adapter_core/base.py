from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from platform_contracts import (
    UNRESOLVED_PREFIX,
    CanonicalShotSpec,
    PromptCompilerOutput,
    forbidden_terms,
)
from pydantic import BaseModel, Field

#: The failures every adapter names in its negative prompt, whichever path
#: compiled the shot. On the Skill path they are merged after the Skill's own
#: negative prompt, so a package that names other risks keeps the platform's
#: guards (the locked style, the canonical product) rather than replacing them.
BASELINE_NEGATIVE_TERMS: tuple[str, ...] = (
    "identity drift",
    "visual style drift",
    "palette drift",
    "altered canonical product",
    "extra subjects",
    "duplicate limbs",
    "extra cuts",
)


class AdapterInput(BaseModel):
    shot: CanonicalShotSpec
    context: dict[str, Any] = Field(default_factory=dict)
    #: The prompt-compiler Skill's package for exactly this shot, when the
    #: Skill compiled it (a verified ``COMPILED`` package). Its positive prompt
    #: is then the body every adapter delivers; without one the adapter
    #: renders the canonical spec itself. A caller passes it only for a
    #: Skill-driven compilation, never for the deterministic package, whose
    #: JSON rendering the canonical lines already replace.
    package: PromptCompilerOutput | None = None

    @property
    def skill_package(self) -> PromptCompilerOutput | None:
        """The package to deliver: a COMPILED one with a positive prompt, else None."""

        package = self.package
        if package is None or package.status != "COMPILED" or not (package.positive_prompt or "").strip():
            return None
        return package


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
    # The cinematography fields a stage may leave empty are printed only when
    # they are set, so a shot the stage never designed renders as it always has.
    lighting = {
        key: value
        for key, value in (shot.get("lighting") or {}).items()
        if not (key in ("motivation", "exposure_intent") and not value)
    }
    camera_line = (
        "Camera: "
        f"position={camera.get('position', 'approved')}; "
        f"movement={camera.get('dominant_movement', 'locked')}; "
        f"speed={camera.get('speed', 'steady')}; path={camera.get('path', 'none')}; "
        f"framing={camera.get('framing', 'approved')}; focus={camera.get('focus', 'subject')}; "
        f"angle={camera.get('angle') or 'eye level'}"
    )
    for label, key in (("height", "height"), ("lens", "lens_intent"), ("depth of field", "depth_of_field")):
        if camera.get(key):
            camera_line += f"; {label}={camera[key]}"
    composition = shot.get("composition") or {}
    lines = [
        f"Shot intent: {shot.get('intent', '')}",
        f"Subjects: {subject_line or 'preserve approved subjects and identities'}",
        f"Start state: {shot.get('start_state', {})}",
        f"Single action: {shot.get('dominant_action', '')}",
        f"Blocking: {shot.get('blocking', {})}",
        camera_line,
        f"Lighting: {lighting}",
        *([f"Atmosphere: {shot['atmosphere']}"] if shot.get("atmosphere") else []),
        *(
            [
                "Composition: "
                + "; ".join(f"{key}={value}" for key, value in composition.items() if value)
            ]
            if any(composition.values())
            else []
        ),
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


def prompt_lines(
    spec: CanonicalShotSpec, context: dict[str, Any], package: PromptCompilerOutput | None
) -> list[str]:
    """The body of the prompt an adapter delivers, before its own model-specific lines.

    With a Skill package (a verified ``COMPILED`` package the prompt-compiler
    Skill produced for this shot) the body is the Skill's positive prompt -
    the paid wording is what the provider receives - followed by what the
    package does not carry by contract: the locked style when the project has
    one, any continuity assertion the Skill wrote but did not restate in the
    prose, and the bounded production context the retrieval stage assembled.
    Without a package the canonical rendering of the spec stands, as it
    always has.
    """

    if package is None or package.status != "COMPILED" or not (package.positive_prompt or "").strip():
        return canonical_lines(spec, context)
    positive = package.positive_prompt or ""
    lines = [positive.strip()]
    if spec.style_lock:
        lines.append(f"Locked visual style: {spec.style_lock}")
    folded = positive.casefold()

    def unrestated(value: str) -> bool:
        text = value.strip()
        return bool(text) and text.casefold() not in folded

    missing_assertions = [item.strip() for item in package.continuity_assertions if unrestated(item)]
    if missing_assertions:
        lines.append("Continuity assertions: " + "; ".join(missing_assertions))
    # What the client approved and forbade for this shot. The package is not
    # required to restate a constraint - only the action, the subjects, the
    # line, the claims and the copy are re-verified in its prose - so a
    # prohibition or an approved staging note would be lost between the
    # deterministic path, which prints them all, and the Skill path. Only the
    # entries the Skill did not already write are appended, and never an
    # `unresolved:` marker, which cannot reach a COMPILED package at all.
    missing_constraints = [
        item.strip()
        for item in spec.constraints
        if not item.strip().lower().startswith(UNRESOLVED_PREFIX) and unrestated(item)
    ]
    if missing_constraints:
        lines.append("Constraints: " + "; ".join(missing_constraints))
    assembled_context = str(context.get("assembled_text") or "").strip()
    if assembled_context:
        lines.append(f"Bounded production context:\n{assembled_context}")
    return lines


def negative_prompt(package: PromptCompilerOutput | None, spec: CanonicalShotSpec | None = None) -> str:
    """The negative prompt delivered: the Skill's, plus the guards and prohibitions it left out.

    The client's forbidden items reach the negative prompt on the
    deterministic path by construction (``compile_input`` appends them). A
    Skill is instructed to name them too but nothing re-verifies that it did,
    so they are merged here as well: a prohibition is the one thing that must
    not be lost by a change of compiler.
    """

    required = [*BASELINE_NEGATIVE_TERMS, *(forbidden_terms(spec) if spec is not None else [])]
    if package is None or package.status != "COMPILED" or not (package.negative_prompt or "").strip():
        return ", ".join(dict.fromkeys(required))
    own = (package.negative_prompt or "").strip().rstrip(",")
    folded = own.casefold()
    extra = [term for term in dict.fromkeys(required) if term.casefold() not in folded]
    return ", ".join([own, *extra]) if extra else own


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
