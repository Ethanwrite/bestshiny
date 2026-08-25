from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from production_domain.models import RetryCategory
from provider_sdk import (
    GenerationProvider,
    ProviderError,
    ProviderHealth,
    ProviderJob,
    ProviderPollIdentity,
    ProviderReferenceMode,
    ProviderSubmission,
    ProviderTrustLevel,
)
from provider_sdk.capabilities import ChatCapability
from provider_sdk.http import ProviderJsonClient, provider_health_metadata
from provider_sdk.transport import (
    LiveProviderSettings,
    ProviderTransport,
    create_provider_transport,
)

# Reviewed "<logical model>[:<mode>]=<dashscope model>" pairs, mirroring the
# Google Flow mapping so one mechanism covers every logical -> runtime model
# translation. An entry without a mode applies to every mode of that logical
# model.
#
# Wan 2.7 is three separate DashScope models, not one model with three
# switches, so the mode decides the model ID:
#
# | Mode | DashScope model            | Accepts                                |
# | ---- | -------------------------- | -------------------------------------- |
# | t2v  | wan2.7-t2v-2026-06-12      | text, optionally images                |
# | i2v  | wan2.7-i2v-2026-04-25      | images, optionally text and audio      |
# | r2v  | wan2.7-r2v-2026-06-12      | image/video references plus text       |
#
# Wan 3.0 is deliberately absent. It is invitation-only Beta, so no runtime
# model ID has been reviewed for it; an unmapped model is rejected here rather
# than posted to DashScope as a guess, the same rule Google Flow follows. An
# operator who holds an invitation declares it in WAN_VIDEO_MODEL_KEYS.
DEFAULT_VIDEO_MODEL_KEYS: dict[str, str] = {
    "wan-2.7:t2v": "wan2.7-t2v-2026-06-12",
    "wan-2.7:i2v": "wan2.7-i2v-2026-04-25",
    "wan-2.7:r2v": "wan2.7-r2v-2026-06-12",
}


def parse_video_model_keys(configured: str) -> dict[str, str]:
    """Parse the operator-reviewed logical-to-DashScope model declaration.

    Only operator entries are returned. They must stay distinguishable from the
    built-in defaults so an explicit WAN2_7_*_MODEL_ID setting can outrank a
    default without outranking a deliberate declaration.
    """

    mapping: dict[str, str] = {}
    for entry in str(configured or "").split(","):
        item = entry.strip()
        if not item:
            continue
        logical, separator, dashscope_model = item.partition("=")
        if not separator or not logical.strip() or not dashscope_model.strip():
            raise ValueError(f"WAN_VIDEO_MODEL_KEYS entry must be model[:mode]=dashscope_model: {item}")
        mapping[logical.strip()] = dashscope_model.strip()
    return mapping


def resolve_video_model(
    requested: str,
    mode: str,
    model_keys: dict[str, str],
    mode_default: str = "",
) -> str:
    """Resolve one logical model plus its mode to a DashScope model ID.

    A logical registry name such as ``wan-2.7`` is not a DashScope model, and a
    mode-scoped setting alone cannot distinguish Wan versions. The mapping is
    consulted first for ``model:mode`` and then for ``model``; an operator's
    mode-specific setting remains an explicit override, and an unmapped model
    is rejected rather than posted to DashScope as an unknown model.
    """

    selected = str(requested or "").strip()
    keys = (f"{selected}:{mode}", selected)
    # 1. an explicit operator declaration always wins;
    for key in keys:
        if selected and key in model_keys:
            return model_keys[key]
    # 2. then the operator's mode-specific setting;
    if mode_default.strip():
        return mode_default.strip()
    # 3. then the reviewed built-in default for a known family.
    for key in keys:
        if selected and key in DEFAULT_VIDEO_MODEL_KEYS:
            return DEFAULT_VIDEO_MODEL_KEYS[key]
    if not selected:
        raise _invalid("a Wan video model is required for this mode")
    raise _invalid(
        f"Wan has no reviewed DashScope model for {selected!r} in {mode} mode; "
        "declare it in WAN_VIDEO_MODEL_KEYS"
    )


# --- The Wan 2.7 media plane -------------------------------------------------
#
# Wan 2.7 carries every non-text input in one ``media`` array. On the wire each
# entry has exactly two official fields, ``type`` and ``url``.
#
# The role — first frame, reference image, reference video — is **canonical
# internal state only and is never serialized**. It still has to exist here: a
# clip whose end the shot continues from, a video the shot takes motion and
# grade reference from, and a still that fixes a character's identity are three
# different instructions, and the role is what lets this adapter choose the
# right model, enforce the per-role limits and order the array deterministically
# before any of that is thrown away at the boundary.
#
# Because the wire carries no role, **position is the only signal the provider
# gets**, so ``_ROLE_ORDER`` below is part of the contract rather than a tidiness
# preference.
#
# The previous payload had the opposite defect in two directions: it flattened
# a first frame to ``img_url`` and a reference video to ``video_url``, and it
# never read ``reference_images``/``reference_urls`` at all — so every reference
# still the Gateway resolved was dropped on the floor, silently, after being
# paid for.


class WanMediaRole(StrEnum):
    """What one entry in ``input.media`` is *for*."""

    FIRST_FRAME = "first_frame"
    LAST_FRAME = "last_frame"
    FIRST_CLIP = "first_clip"
    REFERENCE_IMAGE = "reference_image"
    REFERENCE_VIDEO = "reference_video"
    REFERENCE_VOICE = "reference_voice"


_IMAGE_ROLES = frozenset(
    {WanMediaRole.FIRST_FRAME, WanMediaRole.LAST_FRAME, WanMediaRole.REFERENCE_IMAGE}
)
_VIDEO_ROLES = frozenset({WanMediaRole.FIRST_CLIP, WanMediaRole.REFERENCE_VIDEO})
_AUDIO_ROLES = frozenset({WanMediaRole.REFERENCE_VOICE})

# Which request keys carry which role. The aliases exist because the platform's
# own adapter layer, the Gateway's URL resolution and a hand-written request all
# name the same thing differently; collapsing them here is what keeps the role —
# not the spelling — authoritative.
_SINGLE_ROLE_KEYS: tuple[tuple[WanMediaRole, tuple[str, ...]], ...] = (
    (
        WanMediaRole.FIRST_FRAME,
        ("first_frame", "first_frame_image", "start_frame", "start_frame_url"),
    ),
    (WanMediaRole.LAST_FRAME, ("last_frame", "end_frame", "end_frame_url")),
    (WanMediaRole.FIRST_CLIP, ("first_clip", "first_clip_url", "continuation_video")),
    (WanMediaRole.REFERENCE_VIDEO, ("reference_video", "reference_video_url")),
    (WanMediaRole.REFERENCE_VOICE, ("reference_voice", "reference_voice_url", "voice_reference")),
)
_LIST_ROLE_KEYS: tuple[tuple[WanMediaRole, tuple[str, ...]], ...] = (
    (WanMediaRole.REFERENCE_IMAGE, ("reference_images", "reference_urls")),
    (WanMediaRole.REFERENCE_VIDEO, ("reference_videos",)),
    (WanMediaRole.REFERENCE_VOICE, ("reference_voices",)),
)

# The order entries take in ``media``. The wire carries no role, so this is how
# the provider can tell a first frame from a reference at all: the frame leads.
_ROLE_ORDER: dict[WanMediaRole, int] = {
    WanMediaRole.FIRST_FRAME: 0,
    WanMediaRole.LAST_FRAME: 1,
    WanMediaRole.FIRST_CLIP: 2,
    WanMediaRole.REFERENCE_VIDEO: 3,
    WanMediaRole.REFERENCE_IMAGE: 4,
    WanMediaRole.REFERENCE_VOICE: 5,
}

# What each Wan 2.7 model accepts, per the deployment documentation:
#
#   t2v  text, optionally images
#   i2v  images and a clip to continue from, optionally text
#   r2v  a first frame *together with* reference images/videos
#
# R2V taking a first frame alongside its references is the point of the mode and
# was previously modelled wrongly here: a start frame plus a reference video was
# rejected as inexpressible when it is exactly what R2V is for.
#
# ``REFERENCE_VOICE`` appears in no mode, and that is the *declaration* speaking:
# ``supports_reference_voice`` is false for Wan 2.7. The role and its
# serialization exist anyway, so the day the capability is declared true the
# wire can already carry it — see ``WanMedia.media_type``. What must never
# happen is the pair drifting apart: a profile claiming voice reference while
# the serializer silently drops it is the same defect class as the reference
# images this adapter used to discard. ``test_model_routing_integrity`` binds
# the two together.
#
# A role outside the selected mode's set is rejected rather than dropped. That
# is the difference between "this shot cannot be expressed on this model" and a
# billed generation that quietly ignored half its inputs.
_MODE_ROLES: dict[str, frozenset[WanMediaRole]] = {
    "t2v": frozenset({WanMediaRole.REFERENCE_IMAGE}),
    "i2v": frozenset(
        {
            WanMediaRole.FIRST_FRAME,
            WanMediaRole.LAST_FRAME,
            WanMediaRole.FIRST_CLIP,
            WanMediaRole.REFERENCE_IMAGE,
        }
    ),
    "r2v": frozenset(
        {
            WanMediaRole.FIRST_FRAME,
            WanMediaRole.REFERENCE_IMAGE,
            WanMediaRole.REFERENCE_VIDEO,
        }
    ),
}

# The capability axes this adapter can actually serialize, keyed by the profile
# flag that authorises each. A flag set true with no mode carrying its role is a
# lie the integrity gate refuses.
ROLE_CAPABILITY_FLAG: dict[WanMediaRole, str] = {
    WanMediaRole.FIRST_CLIP: "supports_video_extension",
    WanMediaRole.REFERENCE_IMAGE: "supports_reference_image",
    WanMediaRole.REFERENCE_VIDEO: "supports_v2v",
    WanMediaRole.REFERENCE_VOICE: "supports_reference_voice",
}

_REFERENCE_ROLES = frozenset({WanMediaRole.REFERENCE_IMAGE, WanMediaRole.REFERENCE_VIDEO})

# Published R2V bounds. Enforced locally so an over-long reference list is
# refused before it is billed rather than after.
MAX_FIRST_FRAME = 1
MAX_REFERENCE_ASSETS = 5

# Wan expresses framing through a resolution tier, not through the "720p" label
# the platform's shot spec uses. The previous payload posted that label straight
# into `size`, a field that takes pixel dimensions.
_RESOLUTIONS: dict[str, str] = {
    "480p": "480P",
    "540p": "540P",
    "720p": "720P",
    "1080p": "1080P",
    "1440p": "1440P",
    "2160p": "2160P",
    "4k": "2160P",
}


@dataclass(frozen=True)
class WanMedia:
    """One entry of ``input.media``."""

    role: WanMediaRole
    url: str

    @property
    def media_type(self) -> str:
        if self.role in _VIDEO_ROLES:
            return "video"
        if self.role in _AUDIO_ROLES:
            return "audio"
        return "image"

    def as_payload(self) -> dict[str, str]:
        """The two official fields. The role stays behind, on purpose."""

        return {"type": self.media_type, "url": self.url}


def _fetchable(url: object, role: WanMediaRole) -> str:
    """Wan fetches every reference itself, so anything else is unusable.

    An asset ID or a local path reaching this point means the Gateway did not
    resolve it, and posting it would spend a generation on a reference the
    provider cannot read.
    """

    candidate = str(url).strip()
    if not candidate.lower().startswith(("http://", "https://")):
        raise _invalid(
            f"Wan {role.value} must be a URL the provider can fetch, not {candidate[:60]!r}"
        )
    return candidate


def collect_media(request: dict[str, Any]) -> list[WanMedia]:
    """Every media input in the request, each tagged with its role.

    Order is stable and duplicates are dropped per role, so a start frame that
    also appears in the reference list does not become two entries.
    """

    media: list[WanMedia] = []
    seen: set[tuple[WanMediaRole, str]] = set()

    def add(role: WanMediaRole, value: object) -> None:
        if value in (None, ""):
            return
        url = _fetchable(value, role)
        if (role, url) in seen:
            return
        seen.add((role, url))
        media.append(WanMedia(role, url))

    explicit = request.get("media")
    if isinstance(explicit, list):
        # An operator-supplied media array is authoritative; it is still
        # validated so a malformed role cannot reach DashScope.
        for item in explicit:
            if not isinstance(item, dict):
                raise _invalid("each Wan media entry must be an object")
            try:
                role = WanMediaRole(str(item.get("role") or ""))
            except ValueError as exc:
                raise _invalid(
                    f"unknown Wan media role {item.get('role')!r}; expected one of "
                    + ", ".join(sorted(role.value for role in WanMediaRole))
                ) from exc
            add(role, item.get("url"))
        return media

    for role, keys in _SINGLE_ROLE_KEYS:
        for key in keys:
            if request.get(key):
                add(role, request[key])
                break
    for role, keys in _LIST_ROLE_KEYS:
        for key in keys:
            for item in request.get(key) or []:
                add(role, item)
    return _canonical(media)


def _canonical(media: list[WanMedia]) -> list[WanMedia]:
    """Order the array, then hold it to the published bounds.

    Ordering is load-bearing rather than cosmetic: the role is not serialized,
    so where an entry sits is the only way the provider can tell a first frame
    from a reference. Sorting is stable, so the caller's own order survives
    within a role.
    """

    ordered = sorted(media, key=lambda item: _ROLE_ORDER[item.role])
    frames = sum(1 for item in ordered if item.role is WanMediaRole.FIRST_FRAME)
    if frames > MAX_FIRST_FRAME:
        raise _invalid(f"Wan accepts {MAX_FIRST_FRAME} first frame, not {frames}")
    references = sum(1 for item in ordered if item.role in _REFERENCE_ROLES)
    if references > MAX_REFERENCE_ASSETS:
        raise _invalid(
            f"Wan accepts at most {MAX_REFERENCE_ASSETS} reference assets "
            f"(images and videos together); this shot carries {references}"
        )
    return ordered


def resolve_mode(request: dict[str, Any], media: list[WanMedia]) -> str:
    """Pick the Wan 2.7 model family this shot belongs to.

    Precedence, highest first:

    1. an explicit ``mode`` in the request — the caller has already decided;
    2. any video input, because only R2V ingests video at all;
    3. a first or last frame, which is what I2V exists for;
    4. reference stills, which are R2V's declared purpose — the platform's own
       ``REFERENCE_TO_VIDEO`` policy resolves here rather than to T2V, whose
       optional-images affordance is a secondary one;
    5. text alone.
    """

    requested = str(request.get("mode") or request.get("wan_mode") or "").strip().lower()
    if requested:
        if requested not in _MODE_ROLES:
            raise _invalid(
                f"unknown Wan mode {requested!r}; expected t2v, i2v or r2v"
            )
        return requested
    roles = {item.role for item in media}
    if roles & {WanMediaRole.LAST_FRAME, WanMediaRole.FIRST_CLIP}:
        # Only I2V brackets a shot between two frames, and continuation from a
        # clip is an I2V operation: the clip is what the new footage grows out
        # of, exactly as a first frame is.
        return "i2v"
    if roles & _REFERENCE_ROLES:
        # References — with or without a first frame beside them — are R2V's
        # matrix.
        return "r2v"
    if WanMediaRole.FIRST_FRAME in roles:
        return "i2v"
    return "t2v"


def reject_unsupported_roles(mode: str, media: list[WanMedia]) -> None:
    """Fail closed rather than post a request half of which will be ignored."""

    accepted = _MODE_ROLES[mode]
    unsupported = sorted({item.role.value for item in media if item.role not in accepted})
    if unsupported:
        raise _invalid(
            f"Wan {mode} does not accept {', '.join(unsupported)}; "
            f"it accepts {', '.join(sorted(role.value for role in accepted))}. "
            "Split the shot or select a mode that carries every input."
        )


class WanProvider(GenerationProvider, ChatCapability):
    """Alibaba workspace adapter for OpenAI-compatible chat and Wan 2.7 async video."""

    name = "wan"
    # Wan requires fetchable URLs; DashScope never ingests an upload.
    reference_mode = ProviderReferenceMode.FETCHABLE_URL
    trust_level = ProviderTrustLevel.PRODUCTION

    def __init__(
        self,
        *,
        api_key: str = "",
        openai_base_url: str = "",
        dashscope_base_url: str = "",
        chat_model_id: str = "",
        t2v_model_id: str = "",
        i2v_model_id: str = "",
        r2v_model_id: str = "",
        video_model_keys: str = "",
        timeout_seconds: float = 120,
        transport_settings: LiveProviderSettings | None = None,
        chat_transport: ProviderTransport | None = None,
        video_transport: ProviderTransport | None = None,
    ):
        settings = transport_settings or LiveProviderSettings()
        chat_transport_injected = chat_transport is not None
        video_transport_injected = video_transport is not None
        chat_transport = chat_transport or create_provider_transport(
            settings=settings,
            base_url=openai_base_url or "https://wan-openai.invalid",
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        video_transport = video_transport or create_provider_transport(
            settings=settings,
            base_url=dashscope_base_url or "https://wan-dashscope.invalid",
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        configured = bool(api_key.strip())
        self.chat_client = ProviderJsonClient("wan_chat", chat_transport, api_key_configured=configured)
        self.video_client = ProviderJsonClient("wan", video_transport, api_key_configured=configured)
        self.openai_base_configured = bool(openai_base_url.strip())
        self.dashscope_base_configured = bool(dashscope_base_url.strip())
        self.chat_model_id = chat_model_id.strip()
        self.t2v_model_id = t2v_model_id.strip()
        self.i2v_model_id = i2v_model_id.strip()
        self.r2v_model_id = r2v_model_id.strip()
        self.video_model_keys = parse_video_model_keys(video_model_keys)
        self.configured = bool(self.t2v_model_id or self.i2v_model_id or self.r2v_model_id) and (
            (bool(api_key.strip()) and self.dashscope_base_configured) or video_transport_injected
        )
        self.chat_configured = bool(self.chat_model_id) and (
            (bool(api_key.strip()) and self.openai_base_configured) or chat_transport_injected
        )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = (model or self.chat_model_id).strip()
        if not selected:
            raise _invalid("WAN_CHAT_MODEL_ID is not configured")
        if not self.chat_configured:
            raise ProviderError(
                "Wan OpenAI-compatible chat transport is not configured",
                RetryCategory.PERMANENT_ERROR,
                code="PROVIDER_NOT_CONFIGURED",
            )
        return await self.chat_client.request(
            "POST",
            "/chat/completions",
            json_body={"model": selected, "messages": messages, **(parameters or {})},
            submitted=True,
        )

    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del request, account_id, worker_id
        raise ProviderError(
            "Wan image generation is not exposed by this adapter",
            RetryCategory.INVALID_REQUEST,
            code="CAPABILITY_NOT_SUPPORTED",
        )

    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del account_id, worker_id
        payload = self._video_payload(request)
        data = await self.video_client.request(
            "POST",
            "/services/aigc/video-generation/video-synthesis",
            json_body=payload,
            headers={"X-DashScope-Async": "enable"},
            submitted=True,
        )
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        job_id = data.get("task_id") or output.get("task_id")
        if not job_id:
            raise ProviderError(
                "Wan returned no async task ID",
                RetryCategory.PERMANENT_ERROR,
                code="MISSING_PROVIDER_JOB",
                submitted=True,
            )
        return ProviderSubmission(str(job_id), data)

    def _mode_default(self, mode: str) -> str:
        return {"t2v": self.t2v_model_id, "i2v": self.i2v_model_id, "r2v": self.r2v_model_id}.get(
            mode, ""
        )

    def _video_payload(self, request: dict[str, Any]) -> dict[str, Any]:
        media = collect_media(request)
        mode = resolve_mode(request, media)
        # Before anything is billed: every supplied input must be one this mode
        # can actually carry.
        reject_unsupported_roles(mode, media)
        model = resolve_video_model(
            str(request.get("model") or ""),
            mode,
            self.video_model_keys,
            self._mode_default(mode),
        )
        existing_input = request.get("input")
        if isinstance(existing_input, dict):
            input_value: dict[str, Any] = dict(existing_input)
        else:
            prompt = str(request.get("prompt") or "").strip()
            if not prompt and mode == "t2v":
                raise _invalid("Wan video prompt is required")
            input_value = {}
            if prompt:
                input_value["prompt"] = prompt
            if media:
                input_value["media"] = [item.as_payload() for item in media]
        return {
            "model": model,
            "input": input_value,
            "parameters": _video_parameters(request, mode, media),
        }

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        del asset, account_id, worker_id
        raise ProviderError(
            "Wan adapter requires URL references in the request",
            RetryCategory.INVALID_REQUEST,
            code="CAPABILITY_NOT_SUPPORTED",
        )

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool:
        del provider_media_id, account_id, worker_id
        return False

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
        poll_identity: ProviderPollIdentity | None = None,
    ) -> ProviderJob:
        del account_id, worker_id, poll_identity
        if generation_type != "video":
            raise _invalid("Wan polling only supports video tasks")
        data = await self.video_client.request("GET", f"/tasks/{quote(provider_job_id, safe='')}")
        return _wan_job(provider_job_id, data)

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        del account_id, worker_id
        await self.video_client.request("DELETE", f"/tasks/{quote(provider_job_id, safe='')}")
        return True

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        del account_id, worker_id
        return None

    async def health(self) -> ProviderHealth:
        ok, detail, metadata = provider_health_metadata(self.video_client)
        metadata.update(
            {
                "capabilities": ["openai_chat", "wan2.7_async_video"],
                "openai_base_configured": self.openai_base_configured,
                "dashscope_base_configured": self.dashscope_base_configured,
                "chat_model_configured": bool(self.chat_model_id),
                "chat_transport_configured": self.chat_configured,
                "video_models_configured": bool(self.t2v_model_id or self.i2v_model_id or self.r2v_model_id),
            }
        )
        if not self.configured:
            return ProviderHealth(False, "NOT_CONFIGURED", {**metadata, "status": "NOT_CONFIGURED"})
        return ProviderHealth(ok, detail, metadata)


def _resolution(value: object) -> str:
    """Normalize a framing request to Wan's resolution tier.

    Wan takes a tier — ``720P`` — and this adapter sends nothing else for
    framing. There is no ``size`` field in the request it builds: pixel
    dimensions are not part of the published parameter set, so a caller asking
    for them is told rather than having them quietly dropped or, as the payload
    used to do, having the tier label ``"720p"`` posted into a dimensions field.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    tier = _RESOLUTIONS.get(text.lower())
    if tier:
        return tier
    if text.upper() in set(_RESOLUTIONS.values()):
        return text.upper()
    raise _invalid(
        f"Wan cannot express the requested framing {text!r}; it takes a resolution tier "
        f"({', '.join(sorted(_RESOLUTIONS))}) and no pixel dimensions"
    )


def _video_parameters(request: dict[str, Any], mode: str, media: list[WanMedia]) -> dict[str, Any]:
    """Wan's ``parameters`` block, which differs by mode.

    | Mode | Framing sent |
    | ---- | ------------ |
    | t2v  | ``resolution`` and ``ratio`` |
    | i2v  | ``resolution`` only — the first frame already fixes the aspect |
    | r2v  | ``resolution``, and ``ratio`` only when no first frame is supplied |

    The conditional on R2V follows the same rule I2V does: a supplied first
    frame determines the aspect, so sending a ratio next to it asks for two
    different answers to one question.
    """

    existing = request.get("parameters")
    parameters: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    resolution = _resolution(request.get("resolution"))
    if resolution:
        parameters["resolution"] = resolution
    if request.get("size") not in (None, ""):
        # Kept as an explicit refusal rather than a silent ignore: a caller that
        # asked for exact dimensions did not get them.
        raise _invalid(
            "Wan takes a resolution tier, not a pixel size; remove `size` and set `resolution`"
        )
    carries_first_frame = any(item.role is WanMediaRole.FIRST_FRAME for item in media)
    ratio = str(request.get("ratio") or request.get("aspect_ratio") or "").strip()
    if ratio and (mode == "t2v" or (mode == "r2v" and not carries_first_frame)):
        parameters["ratio"] = ratio
    for source, target in (
        ("duration", "duration"),
        ("seed", "seed"),
        ("prompt_extend", "prompt_extend"),
        ("watermark", "watermark"),
        ("audio", "audio"),
        ("negative_prompt", "negative_prompt"),
    ):
        if request.get(source) is not None:
            parameters[target] = request[source]
    parameters.setdefault("watermark", False)
    return parameters


def _wan_job(provider_job_id: str, data: dict[str, Any]) -> ProviderJob:
    output = data.get("output") if isinstance(data.get("output"), dict) else {}
    raw_status = str(output.get("task_status") or data.get("status") or "PENDING").upper()
    if raw_status == "SUCCEEDED":
        status, progress = "COMPLETED", 1.0
    elif raw_status in {"FAILED", "UNKNOWN"}:
        status, progress = "FAILED", 1.0
    elif raw_status in {"CANCELED", "CANCELLED"}:
        status, progress = "CANCELLED", 1.0
    else:
        status = "RUNNING" if raw_status == "RUNNING" else "QUEUED"
        progress = 0.5 if status == "RUNNING" else 0.0
    results = output.get("results") if isinstance(output.get("results"), list) else []
    first = results[0] if results and isinstance(results[0], dict) else {}
    output_url = output.get("video_url") or first.get("url") or first.get("video_url")
    error = output.get("message") or data.get("message")
    return ProviderJob(
        provider_job_id,
        status,
        progress=progress,
        output_url=str(output_url) if output_url else None,
        output_mime_type="video/mp4" if output_url else None,
        error=str(error) if error and status == "FAILED" else None,
        raw=data,
    )


def _invalid(message: str) -> ProviderError:
    return ProviderError(message, RetryCategory.INVALID_REQUEST, code="INVALID_REQUEST")
