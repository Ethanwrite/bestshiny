from __future__ import annotations

import hashlib
import hmac
import inspect
import os
import time
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .schemas import AnalyzeAccepted, AnalyzeRequest, CallbackEnvelope


async def _call(callable_: Callable[..., Any], *args: Any) -> Any:
    """Invoke a sync or async collaborator from the async handler.

    The Modal deployment passes coroutine functions (`.aio` variants), so no
    blocking Modal client call ever runs on the event loop; test harnesses may
    still pass plain callables.
    """

    result = callable_(*args)
    if inspect.isawaitable(result):
        return await result
    return result

#: In-process delivery attempts before an envelope is handed to the spool.
#: Small on purpose: the worker holds a GPU container while it retries, and
#: the spool's scheduled redelivery owns the long tail.
CALLBACK_ATTEMPTS = 3
CALLBACK_BACKOFF_SECONDS = (1.0, 5.0)


def create_api(
    spawn_job: Callable[[dict[str, Any]], None],
    *,
    claim_job: Callable[[str], bool] | None = None,
) -> FastAPI:
    """The single authenticated endpoint, with idempotent acceptance.

    ``claim_job(job_id) -> bool`` atomically claims a job identity; ``False``
    means this job_id was accepted before, so the request is acknowledged
    (202, ``duplicate: true``) without spawning a second GPU worker for the
    same candidate. Passing ``None`` keeps the previous always-spawn behavior
    for local test harnesses only — the Modal deployment always claims.
    """

    web = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @web.post("/v1/character-evidence/analyze", response_model=AnalyzeAccepted, status_code=202)
    async def analyze(
        request: AnalyzeRequest,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        expected = os.environ.get("CHARACTER_EVIDENCE_API_KEY", "")
        if not expected:
            raise HTTPException(503, "Character Evidence authentication is not configured")
        token = authorization.removeprefix("Bearer ").strip() if authorization else ""
        if not hmac.compare_digest(token, expected):
            raise HTTPException(401, "invalid bearer token")
        if claim_job is not None and not await _call(claim_job, request.job_id):
            return JSONResponse(
                status_code=202,
                content=AnalyzeAccepted(job_id=request.job_id, duplicate=True).model_dump(),
            )
        await _call(spawn_job, request.model_dump(mode="json"))
        return JSONResponse(
            status_code=202,
            content=AnalyzeAccepted(job_id=request.job_id).model_dump(),
        )

    return web


class CallbackRejected(RuntimeError):
    """BestShiny answered 4xx: the envelope itself is refused, not the transport.

    Retrying a rejected envelope can never succeed — the receiver has decided
    (unknown candidate, failed lineage, invalid payload). Redelivery must move
    it to the dead partition instead of spinning on it; on 2026-08-29 exactly
    that spin held the outbox drain past its own timeout for three runs.
    """


def _post_callback(raw: bytes, callback_url: str, signing_key: str) -> None:
    # The signature covers a fresh timestamp per attempt, so a redelivered
    # envelope still verifies inside the receiver's timestamp tolerance.
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(
        signing_key.encode("utf-8"), timestamp.encode("ascii") + b"." + raw, hashlib.sha256
    ).hexdigest()
    response = httpx.post(
        callback_url,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Character-Evidence-Timestamp": timestamp,
            "X-Character-Evidence-Signature": signature,
        },
        timeout=30.0,
        follow_redirects=False,
    )
    if 400 <= response.status_code < 500:
        raise CallbackRejected(f"BestShiny rejected the callback with HTTP {response.status_code}")
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"BestShiny callback failed with HTTP {response.status_code}")


def deliver_callback_once(envelope: CallbackEnvelope) -> None:
    """One delivery attempt, no in-process retries.

    The scheduled outbox drain owns retry pacing across runs; a single bounded
    attempt per item is what keeps one unreachable receiver from holding the
    drain past its own function timeout. Raises ``CallbackRejected`` for a 4xx
    answer and ``RuntimeError`` for transport failures and 5xx answers.
    """

    callback_url = os.environ.get("CHARACTER_EVIDENCE_CALLBACK_URL", "").strip()
    signing_key = os.environ.get("CHARACTER_EVIDENCE_CALLBACK_SIGNING_KEY", "")
    if not callback_url.startswith("https://") or not signing_key:
        raise RuntimeError("signed Character Evidence callback is not configured")
    raw = envelope.model_dump_json().encode("utf-8")
    try:
        _post_callback(raw, callback_url, signing_key)
    except httpx.HTTPError as exc:
        raise RuntimeError("BestShiny callback transport failed") from exc


def deliver_callback(
    envelope: CallbackEnvelope,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Deliver one signed callback, retrying transient failures in-process."""

    callback_url = os.environ.get("CHARACTER_EVIDENCE_CALLBACK_URL", "").strip()
    signing_key = os.environ.get("CHARACTER_EVIDENCE_CALLBACK_SIGNING_KEY", "")
    if not callback_url.startswith("https://") or not signing_key:
        raise RuntimeError("signed Character Evidence callback is not configured")
    raw = envelope.model_dump_json().encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(CALLBACK_ATTEMPTS):
        try:
            _post_callback(raw, callback_url, signing_key)
            return
        except CallbackRejected:
            # A 4xx cannot be retried into a 2xx; hand it straight to the
            # caller (the spool path), where the drain dead-letters it.
            raise
        except (httpx.HTTPError, RuntimeError) as exc:
            last_error = exc
            if attempt < len(CALLBACK_BACKOFF_SECONDS):
                sleep(CALLBACK_BACKOFF_SECONDS[attempt])
    raise RuntimeError("BestShiny callback exhausted in-process retries") from last_error


def deliver_or_spool(
    envelope: CallbackEnvelope,
    spool: Callable[[dict[str, Any]], None],
) -> bool:
    """Deliver, and on failure hand the envelope to a durable spool.

    Returns True when delivered now, False when spooled. The spool is the
    contract that a produced result cannot be lost to one unreachable POST:
    the scheduled redelivery drains it until BestShiny acknowledges.
    """

    try:
        deliver_callback(envelope)
        return True
    except RuntimeError:
        spool({"envelope": envelope.model_dump(mode="json"), "attempts": CALLBACK_ATTEMPTS})
        return False


def failure_envelope(payload: dict[str, Any], exc: Exception) -> CallbackEnvelope:
    # Bound the public callback. Exception types are useful; stack traces and
    # presigned URLs are not callback data.
    return CallbackEnvelope(
        job_id=str(payload.get("job_id", "unknown")),
        project_id=str(payload.get("project_id", "unknown")),
        shot_id=str(payload.get("shot_id", "unknown")),
        status="FAILED",
        error_code=type(exc).__name__[:120],
        error_message="Character Evidence inference failed",
    )


__all__ = [
    "CALLBACK_ATTEMPTS",
    "CallbackRejected",
    "create_api",
    "deliver_callback",
    "deliver_callback_once",
    "deliver_or_spool",
    "failure_envelope",
]
