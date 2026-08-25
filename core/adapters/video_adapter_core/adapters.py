from __future__ import annotations

from typing import Any

from .base import (
    AdapterInput,
    ModelGenerationRequest,
    VideoModelAdapter,
    canonical_lines,
    common_payload,
)


def _result(
    *,
    provider: str,
    model: str,
    prompt: str,
    payload: dict[str, Any],
    context: dict[str, Any],
) -> ModelGenerationRequest:
    assets = [
        *context.get("canonical_asset_ids", []),
        *context.get("reference_images", []),
    ]
    return ModelGenerationRequest(
        provider=provider,
        model=model,
        prompt=prompt,
        negative_prompt=(
            "identity drift, visual style drift, palette drift, altered canonical product, "
            "extra subjects, duplicate limbs, extra cuts"
        ),
        payload=payload,
        asset_bindings=list(dict.fromkeys(asset for asset in assets if asset)),
        continuity_assertions=[
            "canonical identity and product invariants remain unchanged",
            "the end composition equals the approved end state",
            "screen direction and eyelines remain as specified",
        ],
    )


class KlingAdapter(VideoModelAdapter):
    name = "kling"

    def compile(self, model: str, value: AdapterInput) -> ModelGenerationRequest:
        prompt = "\n".join(
            [
                *canonical_lines(value.shot, value.context),
                "Execute continuous physical motion with precise first/last-frame control.",
            ]
        )
        common = common_payload(value.shot, value.context)
        payload = {
            "prompt": prompt,
            "image_url": common["start_frame"],
            "tail_image_url": common["end_frame"],
            "reference_images": common["reference_images"],
            "duration": common["duration"],
            "resolution": common["resolution"],
            "aspect_ratio": common["aspect_ratio"],
            "generate_audio": bool(common["audio"]),
            "style_control": common["style_control"],
        }
        return _result(provider="kling", model=model, prompt=prompt, payload=payload, context=value.context)


class VeoAdapter(VideoModelAdapter):
    name = "veo"

    def compile(self, model: str, value: AdapterInput) -> ModelGenerationRequest:
        prompt = "\n".join(
            [
                *canonical_lines(value.shot, value.context),
                "Use concise spatial language and one continuous physically plausible trajectory.",
            ]
        )
        common = common_payload(value.shot, value.context)
        payload = {
            "prompt": prompt,
            "image": common["start_frame"],
            "last_frame": common["end_frame"],
            "reference_images": common["reference_images"],
            "duration_seconds": common["duration"],
            "resolution": common["resolution"],
            "aspect_ratio": common["aspect_ratio"],
            "generate_audio": bool(common["audio"]),
            "style_control": common["style_control"],
        }
        provider = "google_flow" if model.startswith("flow-") else "veo_official"
        return _result(provider=provider, model=model, prompt=prompt, payload=payload, context=value.context)


class SeedanceAdapter(VideoModelAdapter):
    name = "seedance"

    def compile(self, model: str, value: AdapterInput) -> ModelGenerationRequest:
        prompt = "\n".join(
            [
                *canonical_lines(value.shot, value.context),
                "Preserve complex blocking as ordered temporal beats; never merge another story action.",
            ]
        )
        common = common_payload(value.shot, value.context)
        payload = {
            "prompt": prompt,
            "first_frame_image": common["start_frame"],
            "reference_images": common["reference_images"],
            "reference_video": common["reference_video"],
            "duration": common["duration"],
            "resolution": common["resolution"],
            "aspect_ratio": common["aspect_ratio"],
            "audio": common["audio"],
            "style_control": common["style_control"],
        }
        return _result(
            provider="seedance", model=model, prompt=prompt, payload=payload, context=value.context
        )


class GrokAdapter(VideoModelAdapter):
    name = "grok"

    def compile(self, model: str, value: AdapterInput) -> ModelGenerationRequest:
        gaze_constraints = (
            ["Honor the explicitly approved camera eyeline without changing the approved body orientation."]
            if value.shot.allow_camera_gaze
            else [
                "Maintain the specified eyeline toward the scene target throughout the final second.",
                "Never acknowledge or look into the camera; preserve the approved body orientation.",
            ]
        )
        prompt = "\n".join([*canonical_lines(value.shot, value.context), *gaze_constraints])
        common = common_payload(value.shot, value.context)
        payload = {
            "prompt": prompt,
            "image_url": common["start_frame"],
            "duration": common["duration"],
            "resolution": common["resolution"],
            "aspect_ratio": common["aspect_ratio"],
            "audio": common["audio"],
            "style_control": common["style_control"],
        }
        return _result(provider="grok", model=model, prompt=prompt, payload=payload, context=value.context)


class WanAdapter(VideoModelAdapter):
    name = "wan"

    def compile(self, model: str, value: AdapterInput) -> ModelGenerationRequest:
        prompt = "\n".join(
            [
                *canonical_lines(value.shot, value.context),
                "Keep long-form temporal state explicit and preserve all multimodal references.",
            ]
        )
        common = common_payload(value.shot, value.context)
        # Wan 2.7 distinguishes the roles rather than taking a flat media list:
        # a first frame anchors I2V, a first clip continues from existing
        # footage, and reference stills/videos carry identity into R2V. Naming
        # them separately here is what lets the adapter reject a shot whose
        # inputs no single Wan mode can carry, instead of dropping some of them.
        payload = {
            "prompt": prompt,
            "first_frame": common["start_frame"],
            "last_frame": common["end_frame"],
            "first_clip": common["first_clip"],
            "reference_images": common["reference_images"],
            "reference_video": common["reference_video"],
            "duration": common["duration"],
            # A resolution tier, not a pixel size. Wan reads them differently.
            "resolution": common["resolution"],
            "aspect_ratio": common["aspect_ratio"],
            # Wan 2.7 has no generate-audio switch. Its three audio inputs are
            # all *assets*: a custom track on T2V, audio that drives the
            # performance on I2V, and a timbre reference beside an R2V
            # reference material. `common["audio"]` is the shot's audio design —
            # a dict — and posting it as a parameter is what produced the
            # `"audio": {}` that travelled on every Wan request. It is dropped
            # here; the prompt already carries the audio design in words.
            "audio_url": value.context.get("audio_url"),
            "driving_audio": value.context.get("driving_audio"),
            "reference_voice": value.context.get("reference_voice"),
            "style_control": common["style_control"],
        }
        return _result(provider="wan", model=model, prompt=prompt, payload=payload, context=value.context)
