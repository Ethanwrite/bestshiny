from __future__ import annotations

import random
import time
import uuid
from typing import Any

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


def video_payload(request: dict[str, Any], project_id: str) -> tuple[str, dict[str, Any]]:
    duration = int(request.get("duration") or 8)
    model = str(request.get("model") or "")
    model_key = model if model.startswith("abra_") else f"abra_t2v_{duration}s"
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


def image_payload(request: dict[str, Any], project_id: str) -> tuple[str, dict[str, Any]]:
    item: dict[str, Any] = {
        "clientContext": client_context(project_id),
        "seed": int(request.get("metadata", {}).get("seed") or int(time.time() * 1000) % 1_000_000),
        "structuredPrompt": {"parts": [{"text": request["prompt"]}]},
        "imageAspectRatio": IMAGE_ASPECTS.get(
            str(request.get("aspect_ratio", "9:16")), "IMAGE_ASPECT_RATIO_PORTRAIT"
        ),
        "imageModelName": str(request.get("model") or "NARWHAL"),
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
