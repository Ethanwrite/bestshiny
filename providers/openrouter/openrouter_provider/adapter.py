from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

from production_domain.models import RetryCategory
from provider_sdk import (
    GenerationProvider,
    ProviderError,
    ProviderHealth,
    ProviderInlineOutput,
    ProviderJob,
    ProviderPollIdentity,
    ProviderReferenceConstraints,
    ProviderReferenceMode,
    ProviderSubmission,
    ProviderTrustLevel,
)
from provider_sdk.capabilities import ChatCapability, EmbeddingCapability, ResponsesCapability
from provider_sdk.http import ProviderJsonClient, provider_health_metadata
from provider_sdk.transport import (
    LiveProviderSettings,
    ProviderTransport,
    create_provider_transport,
)

# Only these fields may reach the OpenRouter video API. Everything else in a
# generation request — tenancy, routing, accounting, idempotency, canonical shot
# spec and other internal audit metadata — stays inside the platform.
VIDEO_REQUEST_FIELDS = frozenset(
    {
        "model",
        "prompt",
        "negative_prompt",
        "duration",
        "aspect_ratio",
        "resolution",
        "seed",
        "image_url",
        "tail_image_url",
        "reference_images",
        "reference_video",
        "generate_audio",
        "watermark",
    }
)

# Gateway-resolved reference URLs mapped onto the transport fields above.
VIDEO_REFERENCE_ALIASES = {
    "start_frame_url": "image_url",
    "end_frame_url": "tail_image_url",
    "reference_urls": "reference_images",
}


# Only these fields may reach the OpenRouter Image API (POST /images). The list
# is the documented request schema; every internal field — tenancy, routing,
# accounting, idempotency, style embeddings, canonical shot spec — stays inside
# the platform.
IMAGE_REQUEST_FIELDS = frozenset(
    {
        "model",
        "prompt",
        "n",
        "resolution",
        "aspect_ratio",
        "size",
        "quality",
        "output_format",
        "background",
        "output_compression",
        "seed",
        "input_references",
    }
)

# Gateway-resolved reference URLs, in the order the model should see them.
# They become `input_references` entries; the start frame leads because an edit
# is anchored on it.
IMAGE_REFERENCE_SOURCES = ("start_frame_url", "end_frame_url", "reference_urls")

MAX_IMAGE_OUTPUT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class OpenRouterImageEnvelope:
    """The reviewed execution envelope of one OpenRouter image model.

    Values come from the model's own capability descriptor
    (`GET /api/v1/images/models`). They are recorded here rather than fetched at
    submission time so that a request is rejected locally, before the paid call,
    instead of being billed and refused by the provider.
    """

    max_batch: int
    max_input_references: int
    aspect_ratios: frozenset[str]
    qualities: frozenset[str]
    backgrounds: frozenset[str]


# The Image API normalizes these three enums across providers, so an
# operator-declared model inherits them; only the two per-model counts differ.
IMAGE_ASPECT_RATIOS = frozenset(
    {"1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9", "1:4", "4:1", "2:1", "1:2", "auto"}
)
IMAGE_QUALITIES = frozenset({"auto", "low", "medium", "high"})
IMAGE_BACKGROUNDS = frozenset({"auto", "transparent", "opaque"})


# Reviewed 2026-08-22 against GET /api/v1/images/models.
IMAGE_MODEL_ENVELOPES: dict[str, OpenRouterImageEnvelope] = {
    "openai/gpt-image-2": OpenRouterImageEnvelope(
        max_batch=10,
        max_input_references=16,
        aspect_ratios=frozenset(
            {"1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9", "auto"}
        ),
        qualities=IMAGE_QUALITIES,
        # gpt-image-2 does not offer a transparent background.
        backgrounds=frozenset({"auto", "opaque"}),
    ),
}


def parse_image_model_envelopes(raw: str) -> dict[str, OpenRouterImageEnvelope]:
    """Parse operator-declared envelopes: `model=max_batch:max_references`.

    Built-in reviewed entries cover the models this platform ships with. This
    hook exists so an operator can add a model without a code change, and it
    follows the same fail-closed rule: a model absent from the merged table is
    rejected rather than submitted on guessed limits. Only the two counts are
    declarable — they are what differs per model and what costs money to get
    wrong; the enums come from the API's normalized schema.
    """

    parsed: dict[str, OpenRouterImageEnvelope] = {}
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        model, separator, limits = item.partition("=")
        model = model.strip()
        parts = [part.strip() for part in limits.split(":")]
        if not model or not separator or len(parts) != 2:
            raise ValueError(f"invalid OpenRouter image model envelope: {item!r}")
        try:
            max_batch, max_references = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ValueError(f"invalid OpenRouter image model envelope: {item!r}") from exc
        if max_batch < 1 or max_references < 0:
            raise ValueError(f"invalid OpenRouter image model envelope: {item!r}")
        parsed[model] = OpenRouterImageEnvelope(
            max_batch=max_batch,
            max_input_references=max_references,
            aspect_ratios=IMAGE_ASPECT_RATIOS,
            qualities=IMAGE_QUALITIES,
            backgrounds=IMAGE_BACKGROUNDS,
        )
    return parsed


class OpenRouterProvider(
    GenerationProvider,
    ChatCapability,
    ResponsesCapability,
    EmbeddingCapability,
):
    """One OpenRouter client for chat, Responses, embeddings, and async video."""

    name = "openrouter"
    # OpenRouter proxies fetch references themselves; uploads are unsupported.
    reference_mode = ProviderReferenceMode.FETCHABLE_URL
    trust_level = ProviderTrustLevel.PRODUCTION
    # Declared bounds, not observed generosity. GPT Image 2 currently accepts
    # large references; a model behind the same proxy that does not is the
    # normal case, and an unbounded declaration would only be discovered as a
    # rejection after the call had been billed. 4K and 8 MB is the reviewed
    # envelope; the original is untouched and a derived copy is sent instead.
    reference_constraints = ProviderReferenceConstraints(
        max_pixels=3840 * 2160,
        max_bytes=8 * 1024 * 1024,
        accepted_mime_types=frozenset({"image/png", "image/jpeg", "image/webp"}),
        preferred_mime_type="image/jpeg",
    )

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 120,
        image_model_envelopes: str = "",
        transport_settings: LiveProviderSettings | None = None,
        transport: ProviderTransport | None = None,
    ):
        settings = transport_settings or LiveProviderSettings()
        injected_transport = transport is not None
        transport = transport or create_provider_transport(
            settings=settings,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        self.client = ProviderJsonClient(self.name, transport, api_key_configured=bool(api_key.strip()))
        self.configured = bool(api_key.strip()) or injected_transport
        self.image_envelopes = {
            **IMAGE_MODEL_ENVELOPES,
            **parse_image_model_envelopes(image_model_envelopes),
        }

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.client.request(
            "POST",
            "/chat/completions",
            json_body={"model": _required(model, "model"), "messages": messages, **(parameters or {})},
            submitted=True,
        )

    async def create_response(
        self,
        *,
        model: str,
        input_value: str | list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.client.request(
            "POST",
            "/responses",
            json_body={"model": _required(model, "model"), "input": input_value, **(parameters or {})},
            submitted=True,
        )

    async def create_embeddings(
        self,
        *,
        model: str,
        inputs: str | list[str] | list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.client.request(
            "POST",
            "/embeddings",
            json_body={"model": _required(model, "model"), "input": inputs, **(parameters or {})},
            submitted=True,
        )

    async def list_models(self) -> dict[str, Any]:
        return await self.client.request("GET", "/models")

    async def list_video_models(self) -> dict[str, Any]:
        return await self.client.request("GET", "/videos/models")

    async def list_image_models(self) -> dict[str, Any]:
        return await self.client.request("GET", "/images/models")

    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        """Submit one synchronous image generation or edit.

        `POST /images` answers with the finished images, so there is no remote
        job to poll. The terminal result rides back on the submission and the
        Gateway completes the job through its ordinary path.
        """

        del account_id, worker_id
        payload = {
            key: value
            for key, value in request.items()
            if key in IMAGE_REQUEST_FIELDS and value is not None
        }
        model = _required(str(payload.get("model") or ""), "model")
        payload["model"] = model
        payload["prompt"] = _required(str(payload.get("prompt") or ""), "prompt")
        # `image_count` is the platform's canonical field; `n` is this API's
        # name for it. Translating here keeps the transport name out of the
        # rest of the system, the same way reference URLs are aliased below.
        batch = request.get("image_count")
        if batch is not None and payload.get("n") is None:
            payload["n"] = batch
        envelope = self.image_envelopes.get(model)
        if envelope is None:
            raise ProviderError(
                f"OpenRouter image model has no reviewed execution envelope: {model}",
                RetryCategory.INVALID_REQUEST,
                code="OPENROUTER_IMAGE_MODEL_NOT_REVIEWED",
            )
        references = _image_references(request, payload.get("input_references"))
        if references:
            payload["input_references"] = references
        else:
            payload.pop("input_references", None)
        _assert_within_envelope(model, payload, references, envelope)
        data = await self.client.request("POST", "/images", json_body=payload, submitted=True)
        outputs = _image_outputs(data)
        job_id = str(data.get("id") or f"{model}:{data.get('created') or ''}").strip() or model
        return ProviderSubmission(
            job_id,
            data,
            result=ProviderJob(
                job_id,
                "COMPLETED",
                progress=1.0,
                output_mime_type=outputs[0].mime_type,
                outputs=outputs,
                raw=data,
            ),
        )

    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del account_id, worker_id
        payload = {
            key: value
            for key, value in request.items()
            if key in VIDEO_REQUEST_FIELDS and value is not None
        }
        for resolved, target in VIDEO_REFERENCE_ALIASES.items():
            if request.get(resolved) and not payload.get(target):
                payload[target] = request[resolved]
        payload["model"] = _required(str(payload.get("model") or ""), "model")
        payload["prompt"] = _required(str(payload.get("prompt") or ""), "prompt")
        data = await self.client.request("POST", "/videos", json_body=payload, submitted=True)
        job_id = data.get("id")
        if not job_id:
            raise ProviderError(
                "OpenRouter returned no video job ID",
                RetryCategory.PERMANENT_ERROR,
                code="MISSING_PROVIDER_JOB",
                submitted=True,
            )
        return ProviderSubmission(str(job_id), data)

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        del asset, account_id, worker_id
        raise _unsupported("OpenRouter expects URL/data references in the generation request")

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
        if generation_type == "image":
            # `POST /images` is synchronous and creates no remote job, so a poll
            # can only mean the submission's result was lost in flight. Saying so
            # keeps the paid call in reconciliation instead of inventing a
            # result or authorising a refund for work the provider performed.
            raise ProviderError(
                "OpenRouter image results are returned at submission and cannot be re-fetched",
                RetryCategory.PERMANENT_ERROR,
                code="OPENROUTER_IMAGE_RESULT_NOT_RETRIEVABLE",
                submitted=True,
            )
        if generation_type != "video":
            raise _unsupported("OpenRouter provider polling only supports image and video jobs")
        data = await self.client.request("GET", f"/videos/{quote(provider_job_id, safe='')}")
        return _video_job(provider_job_id, data)

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        """OpenRouter documents no cancel or delete endpoint for a video job.

        The previous implementation issued `DELETE /videos/{id}`, which is not
        part of the API. Reporting a cancellation that never happened would free
        local capacity while the provider kept generating and billing, so this
        reports honestly that the remote job cannot be stopped and leaves it
        under polling until it reaches its own terminal state.
        """

        del provider_job_id, account_id, worker_id
        return False

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        del account_id, worker_id
        return None

    async def health(self) -> ProviderHealth:
        ok, detail, metadata = provider_health_metadata(self.client)
        metadata["capabilities"] = ["chat", "responses", "embeddings", "image", "video"]
        metadata["reviewed_image_models"] = sorted(self.image_envelopes)
        if not self.configured:
            return ProviderHealth(False, "NOT_CONFIGURED", {**metadata, "status": "NOT_CONFIGURED"})
        return ProviderHealth(ok, detail, metadata)


def _image_references(request: dict[str, Any], declared: Any) -> list[dict[str, Any]]:
    """Build `input_references` from an Adapter payload or Gateway-resolved URLs.

    A Passenger request carries no Adapter payload, so the URLs the Gateway
    resolved must still reach the model; otherwise an edit would silently become
    a text-to-image generation against no reference at all.
    """

    references: list[dict[str, Any]] = []
    seen: set[str] = set()

    def append(url: str) -> None:
        candidate = url.strip()
        if not candidate or candidate in seen:
            return
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https", "data"}:
            raise ProviderError(
                "OpenRouter image references must be HTTP(S) or data URLs",
                RetryCategory.INVALID_REQUEST,
                code="PROVIDER_REFERENCE_URL_UNAVAILABLE",
            )
        seen.add(candidate)
        references.append({"type": "image_url", "image_url": {"url": candidate}})

    for entry in declared or []:
        if isinstance(entry, str):
            append(entry)
        elif isinstance(entry, dict):
            url = entry.get("image_url")
            append(str(url.get("url", "")) if isinstance(url, dict) else str(url or ""))
    for field_name in IMAGE_REFERENCE_SOURCES:
        value = request.get(field_name)
        if isinstance(value, str):
            append(value)
        elif isinstance(value, list):
            for item in value:
                append(str(item))
    return references


def _assert_within_envelope(
    model: str,
    payload: dict[str, Any],
    references: list[dict[str, Any]],
    envelope: OpenRouterImageEnvelope,
) -> None:
    """Reject a request the model cannot serve before it is billed."""

    batch = payload.get("n")
    if batch is not None:
        try:
            count = int(batch)
        except (TypeError, ValueError) as exc:
            raise _invalid_image(f"{model} batch size must be an integer") from exc
        if count < 1 or count > envelope.max_batch:
            raise _invalid_image(
                f"{model} supports 1-{envelope.max_batch} images per request, got {count}"
            )
        payload["n"] = count
    if len(references) > envelope.max_input_references:
        raise _invalid_image(
            f"{model} accepts at most {envelope.max_input_references} reference images, "
            f"got {len(references)}"
        )
    for field_name, allowed in (
        ("aspect_ratio", envelope.aspect_ratios),
        ("quality", envelope.qualities),
        ("background", envelope.backgrounds),
    ):
        value = payload.get(field_name)
        if value is not None and str(value) not in allowed:
            raise _invalid_image(
                f"{model} does not support {field_name}={value!r}; allowed: {sorted(allowed)}"
            )


def _image_outputs(data: dict[str, Any]) -> list[ProviderInlineOutput]:
    entries = data.get("data") if isinstance(data.get("data"), list) else []
    outputs: list[ProviderInlineOutput] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        encoded = entry.get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            continue
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderError(
                "OpenRouter returned an image that is not valid base64",
                RetryCategory.PERMANENT_ERROR,
                code="INVALID_PROVIDER_IMAGE",
                submitted=True,
            ) from exc
        if len(content) > MAX_IMAGE_OUTPUT_BYTES:
            raise ProviderError(
                "OpenRouter returned an image larger than the accepted bound",
                RetryCategory.PERMANENT_ERROR,
                code="PROVIDER_IMAGE_TOO_LARGE",
                submitted=True,
            )
        mime_type = str(entry.get("media_type") or "image/png")
        outputs.append(ProviderInlineOutput(content=content, mime_type=mime_type))
    if not outputs:
        raise ProviderError(
            "OpenRouter returned no image data",
            RetryCategory.PERMANENT_ERROR,
            code="MISSING_PROVIDER_OUTPUT",
            submitted=True,
        )
    return outputs


def _invalid_image(message: str) -> ProviderError:
    return ProviderError(message, RetryCategory.INVALID_REQUEST, code="IMAGE_ENVELOPE_EXCEEDED")


def _required(value: str, label: str) -> str:
    if not value.strip():
        raise ProviderError(
            f"{label} is required",
            RetryCategory.INVALID_REQUEST,
            code="INVALID_REQUEST",
        )
    return value.strip()


def _unsupported(message: str) -> ProviderError:
    return ProviderError(message, RetryCategory.INVALID_REQUEST, code="CAPABILITY_NOT_SUPPORTED")


def _video_job(provider_job_id: str, data: dict[str, Any]) -> ProviderJob:
    raw_status = str(data.get("status") or "pending").lower()
    if raw_status in {"completed", "succeeded", "success"}:
        status, progress = "COMPLETED", 1.0
    elif raw_status in {"failed", "error", "expired"}:
        status, progress = "FAILED", 1.0
    elif raw_status in {"cancelled", "canceled"}:
        status, progress = "CANCELLED", 1.0
    else:
        status = "RUNNING" if raw_status in {"running", "processing", "in_progress"} else "QUEUED"
        progress = float(data.get("progress") or 0)
        if progress > 1:
            progress /= 100
    output = data.get("output") if isinstance(data.get("output"), dict) else {}
    # A completed job publishes its artefact in `unsigned_urls`. Reading only the
    # older aliases left a finished video with no output URL, which the Gateway
    # can only report as OUTPUT_URL_MISSING on a call that was already billed.
    unsigned = data.get("unsigned_urls")
    first_unsigned = unsigned[0] if isinstance(unsigned, list) and unsigned else None
    output_url = (
        data.get("output_url")
        or data.get("video_url")
        or data.get("url")
        or output.get("url")
        or first_unsigned
    )
    error = data.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("code")
    return ProviderJob(
        provider_job_id,
        status,
        progress=max(0.0, min(1.0, progress)),
        output_url=str(output_url) if output_url else None,
        output_mime_type="video/mp4" if output_url else None,
        error=str(error) if error else None,
        raw=data,
    )
