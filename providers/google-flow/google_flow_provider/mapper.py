from __future__ import annotations

import random
import re
import time
import uuid
from typing import Any

from production_domain.models import RetryCategory
from provider_sdk import ProviderError

# Explicitly reviewed logical/provider model ID -> Flow runtime video model key.
# ``{duration}`` is substituted with the requested duration in seconds. Only the
# legacy ``veo`` alias has a reviewed key today; every other registered model
# (including ``flow-veo-3.1``) must be declared through FLOW_VIDEO_MODEL_KEYS
# before it can reach Flow. A missing entry fails closed rather than silently
# degrading to a text-to-video key that renders the wrong model.
DEFAULT_VIDEO_MODEL_KEYS: dict[str, str] = {
    "veo": "abra_t2v_{duration}s",
}

# The reviewed Flow image model names. ``NARWHAL`` is the registered
# provider_model_id of ``flow-narwhal-image-internal``; an unreviewed name is
# rejected rather than silently replaced with it.
REVIEWED_IMAGE_MODEL_NAMES: frozenset[str] = frozenset({"NARWHAL"})

_RUNTIME_MODEL_KEY = re.compile(r"^abra_[A-Za-z0-9_.\-]+$")

ASPECTS = {
    "9:16": "VIDEO_ASPECT_RATIO_PORTRAIT",
    "16:9": "VIDEO_ASPECT_RATIO_LANDSCAPE",
    "portrait": "VIDEO_ASPECT_RATIO_PORTRAIT",
    "landscape": "VIDEO_ASPECT_RATIO_LANDSCAPE",
}

IMAGE_ASPECTS = {
    "9:16": "IMAGE_ASPECT_RATIO_PORTRAIT",
    "16:9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "1:1": "IMAGE_ASPECT_RATIO_SQUARE",
}


def client_context(project_id: str) -> dict[str, Any]:
    return {
        "projectId": project_id,
        "tool": "PINHOLE",
        "userPaygateTier": "PAYGATE_TIER_ONE",
        "sessionId": f";{int(time.time() * 1000)}",
        "recaptchaContext": {
            "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            "token": "",
        },
    }


def parse_video_model_keys(configured: str) -> dict[str, str]:
    """Parse the operator-reviewed ``model=runtime_key`` mapping declaration."""

    mapping = dict(DEFAULT_VIDEO_MODEL_KEYS)
    for entry in str(configured or "").split(","):
        item = entry.strip()
        if not item:
            continue
        model, separator, runtime_key = item.partition("=")
        if not separator or not model.strip() or not runtime_key.strip():
            raise ValueError(f"FLOW_VIDEO_MODEL_KEYS entry must be model=runtime_key: {item}")
        mapping[model.strip()] = runtime_key.strip()
    return mapping


def resolve_video_model_key(model: str, duration: int, model_keys: dict[str, str] | None = None) -> str:
    """Map one selected model to its reviewed Flow runtime video model key.

    An explicit ``abra_*`` key passes through unchanged. Anything else must be
    declared in the reviewed mapping; an undeclared logical ID is rejected
    instead of being silently rendered by a different model.
    """

    selected = str(model or "").strip()
    if selected.startswith("abra_"):
        return selected
    mapping = DEFAULT_VIDEO_MODEL_KEYS if model_keys is None else model_keys
    template = mapping.get(selected)
    if not template:
        raise ProviderError(
            f"Google Flow has no reviewed runtime video model key for {selected!r}; "
            "declare it in FLOW_VIDEO_MODEL_KEYS before generating",
            RetryCategory.INVALID_REQUEST,
            code="FLOW_MODEL_KEY_NOT_MAPPED",
        )
    runtime_key = template.format(duration=duration)
    if not _RUNTIME_MODEL_KEY.match(runtime_key):
        raise ProviderError(
            f"Google Flow runtime video model key for {selected!r} is not a valid Flow key: "
            f"{runtime_key!r}",
            RetryCategory.INVALID_REQUEST,
            code="FLOW_MODEL_KEY_INVALID",
        )
    return runtime_key


def video_payload(
    request: dict[str, Any],
    project_id: str,
    model_keys: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    duration = int(request.get("duration") or 8)
    model_key = resolve_video_model_key(str(request.get("model") or ""), duration, model_keys)
    item: dict[str, Any] = {
        "aspectRatio": ASPECTS.get(str(request.get("aspect_ratio", "9:16")), "VIDEO_ASPECT_RATIO_PORTRAIT"),
        "textInput": {"structuredPrompt": {"parts": [{"text": request["prompt"]}]}},
        "videoModelKey": model_key,
        "seed": int(request.get("metadata", {}).get("seed") or random.randint(1, 9999)),
        "metadata": {},
    }
    start_id = request.get("start_frame_provider_media_id")
    end_id = request.get("end_frame_provider_media_id")
    references = request.get("reference_provider_media_ids") or []
    if start_id and end_id:
        endpoint = "/v1/video:batchAsyncGenerateVideoStartAndEndImage"
        item.update(startImage={"mediaId": start_id}, endImage={"mediaId": end_id})
    elif start_id:
        endpoint = "/v1/video:batchAsyncGenerateVideoStartImage"
        item["startImage"] = {"mediaId": start_id}
    elif references:
        endpoint = "/v1/video:batchAsyncGenerateVideoReferenceImages"
        item["referenceImages"] = [
            {"mediaId": media_id, "imageUsageType": "IMAGE_USAGE_TYPE_ASSET"} for media_id in references
        ]
    else:
        endpoint = "/v1/video:batchAsyncGenerateVideoText"
    body = {
        "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
        "clientContext": client_context(project_id),
        "requests": [item],
        "useV2ModelConfig": True,
    }
    return endpoint, body


def resolve_image_model_name(model: str) -> str:
    """Return the reviewed Flow image model name, or fail closed.

    The previous ``model or "NARWHAL"`` default meant an unset or unreviewed
    model silently rendered as NARWHAL. A caller must now name a reviewed model.
    """

    selected = str(model or "").strip()
    if not selected:
        raise ProviderError(
            "Google Flow image generation requires an explicit model",
            RetryCategory.INVALID_REQUEST,
            code="FLOW_IMAGE_MODEL_MISSING",
        )
    if selected not in REVIEWED_IMAGE_MODEL_NAMES:
        raise ProviderError(
            f"Google Flow has no reviewed image model named {selected!r}",
            RetryCategory.INVALID_REQUEST,
            code="FLOW_IMAGE_MODEL_NOT_REVIEWED",
        )
    return selected


def image_payload(request: dict[str, Any], project_id: str) -> tuple[str, dict[str, Any]]:
    item: dict[str, Any] = {
        "clientContext": client_context(project_id),
        "seed": int(request.get("metadata", {}).get("seed") or int(time.time() * 1000) % 1_000_000),
        "structuredPrompt": {"parts": [{"text": request["prompt"]}]},
        "imageAspectRatio": IMAGE_ASPECTS.get(
            str(request.get("aspect_ratio", "9:16")), "IMAGE_ASPECT_RATIO_PORTRAIT"
        ),
        "imageModelName": resolve_image_model_name(str(request.get("model") or "")),
    }
    references = request.get("reference_provider_media_ids") or []
    if references:
        item["imageInputs"] = [
            {"name": media_id, "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"} for media_id in references
        ]
    return f"/v1/projects/{project_id}/flowMedia:batchGenerateImages", {
        "clientContext": client_context(project_id),
        "requests": [item],
    }
