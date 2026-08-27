"""Reading a shot's requirements as a task, a scene and a reference mode.

The router describes a request as fifteen booleans and a profile name. The
posterior describes evidence as one task type, one scenario and one reference
mode. This is the translation, and it lives here rather than in the runtime so
that it can be tested without a database and read without opening the engine.

It takes a *structural* argument — anything with the attributes
``ShotRequirements`` has — so this package stays free of a dependency on the
model registry. The alternative, importing the registry, would make the
evidence layer depend on the routing layer it is supposed to observe.

**Precedence is fixed and explicit.** A request can require character
consistency *and* camera control *and* text rendering, and the evidence for
those three is different. Picking one silently would make the posterior lookup
depend on the order of a dictionary. The order below is by how specific the
requirement is: a scene that only some models can do at all comes before one
that every model attempts.
"""

from __future__ import annotations

from typing import Protocol

from .keys import ConditionBucket, ReferenceMode, Scenario, TaskType


class ShotRequirementsLike(Protocol):
    """The part of ``ShotRequirements`` this module reads.

    Every member is a read-only property rather than a mutable attribute. A
    mutable protocol attribute is invariant, so ``profile: str`` would refuse
    the real ``ShotRequirements``, whose ``profile`` is a ``Literal`` of four
    names — a narrower type, and a perfectly good one to read.
    """

    @property
    def duration(self) -> float: ...
    @property
    def resolution(self) -> str: ...
    @property
    def profile(self) -> str: ...
    @property
    def requires_image_to_video(self) -> bool: ...
    @property
    def requires_start_frame(self) -> bool: ...
    @property
    def requires_end_frame(self) -> bool: ...
    @property
    def requires_reference_images(self) -> bool: ...
    @property
    def requires_multi_reference(self) -> bool: ...
    @property
    def requires_reference_video(self) -> bool: ...
    @property
    def requires_dialogue(self) -> bool: ...
    @property
    def requires_chinese_dialogue(self) -> bool: ...
    @property
    def requires_text_rendering(self) -> bool: ...
    @property
    def requires_character_consistency(self) -> bool: ...
    @property
    def requires_camera_control(self) -> bool: ...
    @property
    def requires_complex_action(self) -> bool: ...
    @property
    def requires_physical_plausibility(self) -> bool: ...


def router_task_type(requirements: ShotRequirementsLike) -> TaskType:
    """What kind of generation this is.

    Checked from the most constraining input down. A request carrying a
    reference video is video-to-video whatever else it also carries, because
    that is the input the provider has to accept.
    """

    if requirements.requires_reference_video:
        return TaskType.V2V
    if requirements.requires_multi_reference or requirements.requires_reference_images:
        return TaskType.R2V
    if requirements.requires_image_to_video or requirements.requires_start_frame:
        return TaskType.I2V
    return TaskType.T2V


def router_reference_mode(requirements: ShotRequirementsLike) -> ReferenceMode:
    if requirements.requires_reference_video:
        return ReferenceMode.REFERENCE_VIDEO
    if requirements.requires_start_frame and requirements.requires_end_frame:
        return ReferenceMode.FIRST_LAST_FRAME
    if requirements.requires_end_frame:
        return ReferenceMode.LAST_FRAME
    if requirements.requires_start_frame or requirements.requires_image_to_video:
        return ReferenceMode.FIRST_FRAME
    if requirements.requires_multi_reference:
        return ReferenceMode.MULTI_REFERENCE
    if requirements.requires_reference_images:
        return ReferenceMode.REFERENCE_IMAGE
    return ReferenceMode.NONE


def router_scenario(requirements: ShotRequirementsLike) -> Scenario:
    """The scene whose evidence should decide this shot.

    One scene, chosen by a fixed precedence rather than by combining several.
    Combining would be averaging across scenes, which is the mixing this whole
    package refuses; choosing the narrowest applicable one is the reading that
    puts the request in the cell most likely to describe it.
    """

    if requirements.requires_chinese_dialogue:
        return Scenario.CHINESE_TEXT
    if requirements.requires_dialogue:
        return Scenario.DIALOGUE_LIPSYNC
    if requirements.requires_text_rendering:
        return Scenario.TEXT_RENDERING
    if requirements.requires_end_frame:
        return Scenario.FIRST_LAST_FRAME
    if requirements.requires_multi_reference or requirements.requires_reference_images:
        return Scenario.REFERENCE_ADHERENCE
    if requirements.profile == "commercial_hero":
        return Scenario.COMMERCIAL_PRODUCT
    if requirements.requires_physical_plausibility:
        return Scenario.PHYSICS
    if requirements.requires_complex_action:
        return Scenario.MOTION
    if requirements.requires_camera_control:
        return Scenario.CAMERA_MOTION
    if requirements.requires_character_consistency:
        return Scenario.IDENTITY
    if requirements.profile == "dialogue":
        return Scenario.DIALOGUE_LIPSYNC
    if requirements.profile == "action":
        return Scenario.MOTION
    return Scenario.GENERIC


def router_conditions(requirements: ShotRequirementsLike) -> ConditionBucket:
    return ConditionBucket(
        duration_bucket=ConditionBucket.bucket_duration(requirements.duration),
        resolution=requirements.resolution,
        reference_mode=router_reference_mode(requirements),
    )


__all__ = [
    "ShotRequirementsLike",
    "router_conditions",
    "router_reference_mode",
    "router_scenario",
    "router_task_type",
]
