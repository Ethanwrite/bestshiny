"""The Cinematography Skill's output contract: HOW WE SEE an approved shot.

The plan carries only photographic decisions - framing, angle, height, lens
intent, depth of field, focus, one dominant camera movement, motivated light,
atmosphere and the start / end composition. It never carries story: a plan
that names an action, a line, a character fact, a product fact, an ending, a
model or a provider has stepped outside the Skill's authority and is refused
as a whole, so the deterministic defaults stand in and the record says why.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Keys a cinematography plan may never carry: they belong to Director, Shot
#: Planner, Continuity or Model Router. Checked before validation so an
#: out-of-authority decision is refused rather than silently dropped.
CINEMATOGRAPHY_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "action",
        "dominant_action",
        "dialogue",
        "line",
        "plot",
        "story",
        "ending",
        "characters",
        "identity",
        "product",
        "product_claims",
        "provider",
        "model",
        "model_id",
        "provider_model_id",
        "shots",
        "shot_count",
        "start_state",
        "end_state",
    }
)


#: The string limits below bound the prompt the compiler renders from a plan;
#: they are not a style guide. A real focus pull or camera path runs past 160
#: characters, and a plan refused for its wording costs the shot its design.


class CinematographyCamera(BaseModel):
    model_config = ConfigDict(extra="ignore")

    framing: str = Field(default="medium", min_length=1, max_length=200)
    angle: str = Field(default="eye level", min_length=1, max_length=200)
    height: str = Field(default="subject eye height", max_length=200)
    position: str = Field(default="approved position", min_length=1, max_length=400)
    dominant_movement: str = Field(default="locked-off", min_length=1, max_length=200)
    speed: str = Field(default="steady", max_length=200)
    path: str = Field(default="none", max_length=400)
    focus: str = Field(default="primary subject", max_length=400)
    screen_axis: str = Field(default="preserve established axis", max_length=200)
    lens_intent: str = Field(default="natural perspective", max_length=400)
    depth_of_field: str = Field(default="moderate", max_length=200)

    @field_validator("dominant_movement")
    @classmethod
    def _one_movement(cls, value: str) -> str:
        lowered = value.lower()
        if any(token in lowered for token in (" and ", " + ", " then ", ", then", " plus ", "然后", "并")):
            raise ValueError("one dominant camera movement only; two moves are two shots")
        return value.strip()


class CinematographyLighting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    direction: str = Field(default="preserve established direction", min_length=1, max_length=400)
    quality: str = Field(default="preserve established quality", min_length=1, max_length=400)
    contrast: str = Field(default="preserve established contrast", min_length=1, max_length=200)
    color_temperature: str = Field(
        default="preserve established color temperature", min_length=1, max_length=200
    )
    practicals: list[str] = Field(default_factory=list, max_length=12)
    motivation: str = Field(default="", max_length=400)
    exposure_intent: str = Field(default="", max_length=400)


class CinematographyPlan(BaseModel):
    """Exactly the photographic treatment of one approved shot."""

    model_config = ConfigDict(extra="ignore")

    camera: CinematographyCamera = Field(default_factory=CinematographyCamera)
    lighting: CinematographyLighting = Field(default_factory=CinematographyLighting)
    subject_positions: dict[str, str] = Field(default_factory=dict)
    atmosphere: str = Field(default="", max_length=600)
    start_composition: str = Field(default="", max_length=800)
    end_composition: str = Field(default="", max_length=800)
    continuity_checks: list[str] = Field(default_factory=list, max_length=20)
    unresolved: list[str] = Field(default_factory=list, max_length=20)

    def camera_values(self) -> dict[str, Any]:
        """The camera fields in the CanonicalCameraSpec vocabulary."""

        return {
            "position": self.camera.position,
            "angle": self.camera.angle,
            "framing": self.camera.framing,
            "dominant_movement": self.camera.dominant_movement,
            "speed": self.camera.speed,
            "path": self.camera.path,
            "focus": self.camera.focus,
            "screen_axis": self.camera.screen_axis,
        }

    def lighting_values(self) -> dict[str, Any]:
        return {
            "direction": self.lighting.direction,
            "quality": self.lighting.quality,
            "contrast": self.lighting.contrast,
            "color_temperature": self.lighting.color_temperature,
            "practicals": list(self.lighting.practicals),
        }


def cinematography_authority_violations(raw: dict[str, Any]) -> list[str]:
    """Out-of-authority keys anywhere in a raw plan, as ``path`` strings."""

    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_text = str(key)
                here = f"{path}.{key_text}" if path else key_text
                if key_text.lower() in CINEMATOGRAPHY_FORBIDDEN_KEYS:
                    found.append(here)
                    continue
                walk(value, here)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(raw, "")
    return found
