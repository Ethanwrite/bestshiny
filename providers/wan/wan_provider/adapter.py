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
    ProviderReferenceConstraints,
    ProviderReferenceMode,
    ProviderSubmission,
    ProviderTrustLevel,
    VideoReferenceConstraints,
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
# | Mode | DashScope model            | Accepts                                  |
# | ---- | -------------------------- | ---------------------------------------- |
# | t2v  | wan2.7-t2v-2026-06-12      | text, optionally one custom audio track  |
# | i2v  | wan2.7-i2v-2026-04-25      | a first frame or clip, a last frame, and |
# |      |                            | driving audio — never a reference image  |
# | r2v  | wan2.7-r2v-2026-06-12      | image/video references, optionally one   |
# |      |                            | first frame, plus text                   |
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
# Wan 2.7 carries every non-text input of I2V and R2V in one ``media`` array,
# and each entry names its own semantic role in ``type``:
#
#     {"type": "first_frame",     "url": ...}
#     {"type": "last_frame",      "url": ...}
#     {"type": "first_clip",      "url": ...}
#     {"type": "driving_audio",   "url": ...}
#     {"type": "reference_image", "url": ..., "reference_voice": ...}
#     {"type": "reference_video", "url": ..., "reference_voice": ...}
#
# The role **is** the wire contract. An earlier version of this adapter mapped
# the role down to a media *category* — ``image``/``video``/``audio`` — on the
# theory that the role was internal state and array position was the only
# signal the provider received. That is the opposite of the published protocol:
# the official I2V and R2V references define these exact strings as the values
# of ``media.type``, and nothing about a request's meaning is carried by
# position. A first frame posted as ``{"type": "image"}`` is not a first frame
# that arrived unlabelled; it is a request DashScope rejects.
#
# T2V has no ``media`` array at all. Its only non-text input is an optional
# custom audio track at ``input.audio_url``.
#
# ``negative_prompt`` belongs to ``input`` in all three modes, beside ``prompt``.
# It is not a ``parameters`` field.
#
# Sources: the Alibaba Model Studio T2V, I2V and R2V API references.


class WanMediaRole(StrEnum):
    """One entry of ``input.media`` — and the ``type`` it carries on the wire."""

    FIRST_FRAME = "first_frame"
    LAST_FRAME = "last_frame"
    FIRST_CLIP = "first_clip"
    DRIVING_AUDIO = "driving_audio"
    REFERENCE_IMAGE = "reference_image"
    REFERENCE_VIDEO = "reference_video"


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
    # Deliberately *not* aliased to ``audio_url``: that is T2V's custom audio
    # track, a different field of a different mode. Conflating them would turn
    # "drive this character's performance from this take" into "play this over
    # the top", silently.
    (WanMediaRole.DRIVING_AUDIO, ("driving_audio", "driving_audio_url")),
    (WanMediaRole.REFERENCE_VIDEO, ("reference_video", "reference_video_url")),
)
_LIST_ROLE_KEYS: tuple[tuple[WanMediaRole, tuple[str, ...]], ...] = (
    (WanMediaRole.REFERENCE_IMAGE, ("reference_images", "reference_urls")),
    (WanMediaRole.REFERENCE_VIDEO, ("reference_videos",)),
)

# The voice timbre of the subject in a reference material. R2V nests it *inside*
# the reference entry rather than carrying it as its own media entry, so it has
# no role of its own — it is a property of a reference_image or reference_video.
_VOICE_KEYS: tuple[str, ...] = ("reference_voice", "reference_voice_url", "voice_reference")

# T2V's custom audio track. Its own ``input`` field, not a media entry.
_AUDIO_URL_KEYS: tuple[str, ...] = ("audio_url", "custom_audio", "custom_audio_url")

# The order entries take in ``media``. Position carries no meaning now that the
# role is serialized, so this is determinism rather than protocol: one request
# shape produces one payload, which is what makes idempotency keys and recorded
# fixtures stable.
_ROLE_ORDER: dict[WanMediaRole, int] = {
    WanMediaRole.FIRST_FRAME: 0,
    WanMediaRole.LAST_FRAME: 1,
    WanMediaRole.FIRST_CLIP: 2,
    WanMediaRole.REFERENCE_VIDEO: 3,
    WanMediaRole.REFERENCE_IMAGE: 4,
    WanMediaRole.DRIVING_AUDIO: 5,
}

# What each Wan 2.7 model accepts, per the published API references:
#
#   t2v  text and an optional custom audio track — no media array whatsoever
#   i2v  a first frame or a first clip, optionally a last frame, optionally
#        driving audio
#   r2v  reference images/videos, optionally alongside one first frame
#
# Two entries here were wrong before and both were wrong in the same direction —
# they advertised a reference image on a mode that has none. T2V's HTTP API
# takes ``prompt``, ``negative_prompt`` and ``audio_url``: there is nowhere for
# an image to go. I2V's material combinations are enumerated by the provider
# and ``reference_image`` is not among them. A shot routed to either of those
# with reference stills attached would have been billed with its references
# discarded — the exact failure this table exists to prevent.
#
# A role outside the selected mode's set is rejected rather than dropped. That
# is the difference between "this shot cannot be expressed on this model" and a
# billed generation that quietly ignored half its inputs.
_MODE_ROLES: dict[str, frozenset[WanMediaRole]] = {
    "t2v": frozenset(),
    "i2v": frozenset(
        {
            WanMediaRole.FIRST_FRAME,
            WanMediaRole.LAST_FRAME,
            WanMediaRole.FIRST_CLIP,
            WanMediaRole.DRIVING_AUDIO,
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

# I2V does not accept an arbitrary subset of its roles: the provider publishes a
# closed list of material combinations. Holding it here is what stops a
# plausible-looking request — driving audio with nothing to drive, a last frame
# with no first — from being billed before the provider refuses it.
_I2V_COMBINATIONS: tuple[frozenset[WanMediaRole], ...] = (
    frozenset({WanMediaRole.FIRST_FRAME}),
    frozenset({WanMediaRole.FIRST_FRAME, WanMediaRole.DRIVING_AUDIO}),
    frozenset({WanMediaRole.FIRST_FRAME, WanMediaRole.LAST_FRAME}),
    frozenset({WanMediaRole.FIRST_FRAME, WanMediaRole.LAST_FRAME, WanMediaRole.DRIVING_AUDIO}),
    frozenset({WanMediaRole.FIRST_CLIP}),
    frozenset({WanMediaRole.FIRST_CLIP, WanMediaRole.LAST_FRAME}),
)

# The capability axes this adapter can actually serialize, keyed by the profile
# flag that authorises each. A flag set true with no mode carrying its role is a
# lie the integrity gate refuses.
#
# ``supports_reference_voice`` is documented on the profile as "a voice or audio
# asset the model conditions *on*", which is precisely what driving audio and an
# R2V voice reference both are — as distinct from ``supports_audio``, which is
# audio the model produces. One flag therefore authorises both audio-in axes.
ROLE_CAPABILITY_FLAG: dict[WanMediaRole, str] = {
    WanMediaRole.FIRST_CLIP: "supports_video_extension",
    WanMediaRole.DRIVING_AUDIO: "supports_reference_voice",
    WanMediaRole.REFERENCE_IMAGE: "supports_reference_image",
    WanMediaRole.REFERENCE_VIDEO: "supports_v2v",
}

# The nested ``reference_voice`` rides the same declaration as driving audio.
VOICE_CAPABILITY_FLAG = "supports_reference_voice"

_REFERENCE_ROLES = frozenset({WanMediaRole.REFERENCE_IMAGE, WanMediaRole.REFERENCE_VIDEO})

# Published R2V bounds. Enforced locally so an over-long reference list is
# refused before it is billed rather than after.
MAX_FIRST_FRAME = 1
MAX_REFERENCE_ASSETS = 5
MIN_REFERENCE_ASSETS = 1

# Published duration bounds, in seconds. Wan 2.7 takes whole seconds only.
#
# The floor was declared as 1 and is 2; a shot asking for a single second was
# routed here and refused by the provider. The ceiling is not one number: R2V
# carrying a reference *video* tops out at 10 rather than 15, so it depends on
# the request and cannot live in a static profile field alone.
MIN_DURATION = 2
MAX_DURATION = 15
MAX_DURATION_WITH_REFERENCE_VIDEO = 10

# Wan expresses framing through a resolution tier, not through the "720p" label
# the platform's shot spec uses. The previous payload posted that label straight
# into `size`, a field that takes pixel dimensions.
# The tiers Wan 2.7 actually accepts, and only those. An earlier version of this
# table carried 480P, 540P, 1440P and 2160P as a generic normalisation map; a
# live submission was accepted and then failed with
# `Input should be '1080P' or '720P': parameters.resolution`, which is a wasted
# round trip for something knowable here. It matches `supported_resolutions` in
# the registry profile, and `test_model_routing_integrity` holds the two together.
_RESOLUTIONS: dict[str, str] = {
    "720p": "720P",
    "1080p": "1080P",
}


@dataclass(frozen=True)
class WanMedia:
    """One entry of ``input.media``."""

    role: WanMediaRole
    url: str
    # R2V only: the audio URL fixing the timbre of the subject in this
    # reference material. Nested here because that is where the protocol puts
    # it — it is not a media entry of its own.
    reference_voice: str = ""

    def as_payload(self) -> dict[str, str]:
        """The role is the wire contract, not internal state."""

        payload = {"type": self.role.value, "url": self.url}
        if self.reference_voice:
            payload["reference_voice"] = self.reference_voice
        return payload


def _fetchable(url: object, role: WanMediaRole | str) -> str:
    """Wan fetches every reference itself, so anything else is unusable.

    An asset ID or a local path reaching this point means the Gateway did not
    resolve it, and posting it would spend a generation on a reference the
    provider cannot read.
    """

    label = role.value if isinstance(role, WanMediaRole) else str(role)
    candidate = str(url).strip()
    if not candidate.lower().startswith(("http://", "https://")):
        raise _invalid(
            f"Wan {label} must be a URL the provider can fetch, not {candidate[:60]!r}"
        )
    return candidate


def collect_media(request: dict[str, Any]) -> list[WanMedia]:
    """Every media input in the request, each tagged with its role.

    Order is stable and duplicates are dropped per role, so a start frame that
    also appears in the reference list does not become two entries.
    """

    media: list[WanMedia] = []
    seen: set[tuple[WanMediaRole, str]] = set()

    def add(role: WanMediaRole, value: object, voice: object = None) -> None:
        if value in (None, ""):
            return
        url = _fetchable(value, role)
        if (role, url) in seen:
            return
        seen.add((role, url))
        media.append(
            WanMedia(
                role,
                url,
                _voice_for(role, voice) if voice not in (None, "") else "",
            )
        )

    explicit = request.get("media")
    if isinstance(explicit, list):
        # An operator-supplied media array is authoritative; it is still
        # validated so a malformed role cannot reach DashScope. Both spellings
        # are read: ``role`` is this platform's internal name for the field and
        # ``type`` is the provider's, and since the fix that made them the same
        # string a caller writing the wire form directly is not wrong.
        for item in explicit:
            if not isinstance(item, dict):
                raise _invalid("each Wan media entry must be an object")
            named = str(item.get("role") or item.get("type") or "")
            try:
                role = WanMediaRole(named)
            except ValueError as exc:
                raise _invalid(
                    f"unknown Wan media role {named!r}; expected one of "
                    + ", ".join(sorted(role.value for role in WanMediaRole))
                ) from exc
            add(role, item.get("url"), item.get("reference_voice"))
        return _canonical(media)

    for role, keys in _SINGLE_ROLE_KEYS:
        for key in keys:
            if request.get(key):
                add(role, request[key])
                break
    for role, keys in _LIST_ROLE_KEYS:
        for key in keys:
            for item in request.get(key) or []:
                add(role, item)
    return _attach_voice(request, _canonical(media))


def _voice_for(role: WanMediaRole, value: object) -> str:
    """A voice reference is a property of a *reference* material, nothing else."""

    if role not in _REFERENCE_ROLES:
        raise _invalid(
            f"Wan carries reference_voice on a reference image or video, not on {role.value}"
        )
    return _fetchable(value, "reference_voice")


def _attach_voice(request: dict[str, Any], media: list[WanMedia]) -> list[WanMedia]:
    """Bind a flat ``reference_voice`` to the reference material it describes.

    The flat request key exists because the platform's own compiler resolves one
    voice per shot. It is only unambiguous while the shot carries one reference
    material; with several, which subject's timbre it fixes is a question this
    adapter cannot answer, so it asks rather than guessing — a voice attached to
    the wrong plate is a billed generation with the wrong character speaking.
    """

    voice: object = None
    for key in _VOICE_KEYS:
        if request.get(key):
            voice = request[key]
            break
    if voice in (None, ""):
        return media
    targets = [index for index, item in enumerate(media) if item.role in _REFERENCE_ROLES]
    if not targets:
        raise _invalid(
            "Wan reference_voice fixes the timbre of a subject in a reference image or "
            "video; this request carries neither"
        )
    if len(targets) > 1:
        raise _invalid(
            "Wan reference_voice belongs to one reference material and this request carries "
            f"{len(targets)}; send `media` entries with their own reference_voice instead"
        )
    index = targets[0]
    resolved = _voice_for(media[index].role, voice)
    return [
        WanMedia(item.role, item.url, resolved) if position == index else item
        for position, item in enumerate(media)
    ]


def _canonical(media: list[WanMedia]) -> list[WanMedia]:
    """Order the array, then hold it to the published bounds.

    Ordering is no longer load-bearing for meaning — every entry names its own
    role — but it keeps one request shape producing one payload. Sorting is
    stable, so the caller's own order survives within a role.
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
    2. reference stills or videos, which are R2V's declared purpose — the
       platform's own ``REFERENCE_TO_VIDEO`` policy resolves here;
    3. any frame, clip or driving audio, which is I2V's matrix;
    4. text alone.

    Driving audio deliberately resolves to I2V rather than to T2V's custom audio
    track. They are different instructions — one drives a performance, the other
    plays over the result — and a request that names the first gets the first or
    is refused by the combination check, never quietly downgraded to the second.
    """

    requested = str(request.get("mode") or request.get("wan_mode") or "").strip().lower()
    if requested:
        if requested not in _MODE_ROLES:
            raise _invalid(
                f"unknown Wan mode {requested!r}; expected t2v, i2v or r2v"
            )
        return requested
    roles = {item.role for item in media}
    if roles & _REFERENCE_ROLES:
        # References — with or without a first frame beside them — are R2V's
        # matrix.
        return "r2v"
    if roles:
        return "i2v"
    return "t2v"


def reject_unsupported_roles(mode: str, media: list[WanMedia]) -> None:
    """Fail closed rather than post a request half of which will be ignored."""

    accepted = _MODE_ROLES[mode]
    unsupported = sorted({item.role.value for item in media if item.role not in accepted})
    if not unsupported:
        return
    if not accepted:
        raise _invalid(
            f"Wan {mode} accepts no media at all — it takes a prompt, a negative prompt "
            f"and an optional audio_url — but this request carries {', '.join(unsupported)}. "
            "Select a mode that carries every input."
        )
    raise _invalid(
        f"Wan {mode} does not accept {', '.join(unsupported)}; "
        f"it accepts {', '.join(sorted(role.value for role in accepted))}. "
        "Split the shot or select a mode that carries every input."
    )


def reject_unsupported_combination(mode: str, media: list[WanMedia]) -> None:
    """Hold the request to the provider's published material combinations.

    Role membership alone is not the whole rule. I2V enumerates its valid sets,
    and R2V requires at least one reference material — a request that satisfies
    neither is refused here rather than after it is billed.
    """

    roles = {item.role for item in media}
    if mode == "i2v":
        if roles in _I2V_COMBINATIONS:
            return
        combinations = "; ".join(
            " + ".join(sorted(role.value for role in combination))
            for combination in _I2V_COMBINATIONS
        )
        carried = ", ".join(sorted(role.value for role in roles)) or "no media"
        raise _invalid(
            f"Wan i2v accepts only these material combinations: {combinations}. "
            f"This request carries {carried}."
        )
    if mode == "r2v":
        references = sum(1 for item in media if item.role in _REFERENCE_ROLES)
        if references < MIN_REFERENCE_ASSETS:
            raise _invalid(
                "Wan r2v needs at least one reference image or reference video; a first "
                "frame on its own is an i2v shot"
            )


def max_duration_for(mode: str, media: list[WanMedia]) -> int:
    """The ceiling this *particular* request is held to.

    R2V carrying a reference video tops out at 10 seconds where everything else
    reaches 15. A single declared maximum cannot express that, which is why the
    check is here and not only in the registry profile.
    """

    if mode == "r2v" and any(item.role is WanMediaRole.REFERENCE_VIDEO for item in media):
        return MAX_DURATION_WITH_REFERENCE_VIDEO
    return MAX_DURATION


def _duration(value: object, mode: str, media: list[WanMedia]) -> int:
    """Whole seconds inside the bound that applies to this request."""

    if isinstance(value, bool):
        raise _invalid("Wan duration is a whole number of seconds")
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise _invalid(f"Wan duration is a whole number of seconds, not {value!r}") from exc
    if seconds != int(seconds):
        raise _invalid(f"Wan duration is a whole number of seconds, not {value!r}")
    ceiling = max_duration_for(mode, media)
    whole = int(seconds)
    if whole < MIN_DURATION or whole > ceiling:
        reason = (
            " (an r2v shot carrying a reference video tops out there, not at "
            f"{MAX_DURATION})"
            if ceiling == MAX_DURATION_WITH_REFERENCE_VIDEO
            else ""
        )
        raise _invalid(
            f"Wan {mode} takes a duration of {MIN_DURATION}-{ceiling} seconds{reason}; "
            f"this shot asks for {whole}"
        )
    return whole


class WanProvider(GenerationProvider, ChatCapability):
    """Alibaba workspace adapter for OpenAI-compatible chat and Wan 2.7 async video."""

    name = "wan"
    # Wan requires fetchable URLs; DashScope never ingests an upload.
    reference_mode = ProviderReferenceMode.FETCHABLE_URL
    trust_level = ProviderTrustLevel.PRODUCTION
    # Documented bounds from Alibaba Cloud Model Studio's own API references,
    # read 2026-08-29 — never inferred from behaviour:
    #
    # - Reference images / first frame ("Wan2.7 image-to-video" and
    #   "reference-to-video" pages): JPEG/JPG/PNG/BMP/WEBP, up to 20 MB.
    #   (The pages bound each *side* at 240..8,000 px; the image schema here
    #   expresses a total-pixel cap only, so 8000x8000 is declared as the
    #   pixel ceiling and the per-side minimum remains unexpressed — an
    #   undersized plate still fails at the provider. Recorded residual.)
    # - Reference video ("Wan2.7 reference-to-video", type=reference_video):
    #   MP4 or MOV, 1..30 s, each side 240..4,096 px, aspect ratio 1:8..8:1,
    #   up to 100 MB. Codec and frame rate are NOT documented there: no codec
    #   bound is invented — h264 is declared as *our* transcode target inside
    #   the documented containers (DashScope's own outputs are h264 MP4), and
    #   frame rate stays unchecked. "MB" is read as decimal — the stricter
    #   reading, so a copy we pass can never exceed the documented cap under
    #   either interpretation.
    reference_constraints = ProviderReferenceConstraints(
        max_pixels=8000 * 8000,
        max_bytes=20_000_000,
        accepted_mime_types=frozenset(
            {"image/jpeg", "image/png", "image/bmp", "image/webp"}
        ),
        preferred_mime_type="image/jpeg",
        video=VideoReferenceConstraints(
            accepted_containers=frozenset({"video/mp4", "video/quicktime"}),
            preferred_container="video/mp4",
            accepted_codecs=frozenset({"h264"}),
            preferred_codec="h264",
            min_aspect_ratio="1:8",
            max_aspect_ratio="8:1",
            min_width=240,
            min_height=240,
            max_width=4096,
            max_height=4096,
            min_duration_seconds=1.0,
            max_duration_seconds=30.0,
            max_bytes=100_000_000,
        ),
    )

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
        # can actually carry, in a combination the provider publishes.
        reject_unsupported_roles(mode, media)
        reject_unsupported_combination(mode, media)
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
            # `input`, not `parameters`. All three modes put it beside `prompt`.
            negative_prompt = str(request.get("negative_prompt") or "").strip()
            if negative_prompt:
                input_value["negative_prompt"] = negative_prompt
            if media:
                input_value["media"] = [item.as_payload() for item in media]
            audio_url = _t2v_audio_url(request, mode)
            if audio_url:
                input_value["audio_url"] = audio_url
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


def _t2v_audio_url(request: dict[str, Any], mode: str) -> str:
    """T2V's custom audio track, which lives at ``input.audio_url``.

    It exists on T2V and nowhere else. A request that carries one while routing
    to I2V or R2V is refused rather than stripped: those modes carry audio too,
    but as ``driving_audio`` and ``reference_voice``, and the three are not
    interchangeable instructions.
    """

    for key in _AUDIO_URL_KEYS:
        if request.get(key):
            if mode != "t2v":
                raise _invalid(
                    f"Wan {mode} has no audio_url; it carries audio as "
                    + (
                        "a driving_audio media entry"
                        if mode == "i2v"
                        else "reference_voice on a reference material"
                    )
                )
            return _fetchable(request[key], "audio_url")
    return ""


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

    Two fields this block used to carry are gone, because neither is a
    ``parameters`` field in any published mode. ``negative_prompt`` belongs to
    ``input``. ``audio`` was never a field at all: it was the shot's audio
    *design* — a dict — posted verbatim, so every Wan request went out carrying
    ``"audio": {}``. Wan's audio inputs are ``input.audio_url``,
    ``driving_audio`` and ``reference_voice``, all of which are URLs and none of
    which lives here.
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
    if request.get("audio") not in (None, "", {}):
        raise _invalid(
            "Wan has no `audio` parameter; send `audio_url` on t2v, `driving_audio` on i2v, "
            "or `reference_voice` beside an r2v reference material"
        )
    carries_first_frame = any(item.role is WanMediaRole.FIRST_FRAME for item in media)
    ratio = str(request.get("ratio") or request.get("aspect_ratio") or "").strip()
    if ratio and (mode == "t2v" or (mode == "r2v" and not carries_first_frame)):
        parameters["ratio"] = ratio
    if request.get("duration") is not None:
        parameters["duration"] = _duration(request["duration"], mode, media)
    for source, target in (
        ("seed", "seed"),
        ("prompt_extend", "prompt_extend"),
        ("watermark", "watermark"),
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
