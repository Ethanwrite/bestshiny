from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .schemas import AnalyzeAccepted, AnalyzeRequest, CallbackEnvelope


def create_api(spawn_job: Callable[[dict[str, Any]], None]) -> FastAPI:
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
        spawn_job(request.model_dump(mode="json"))
        return JSONResponse(
            status_code=202,
            content=AnalyzeAccepted(job_id=request.job_id).model_dump(),
        )

    return web


def deliver_callback(envelope: CallbackEnvelope) -> None:
    callback_url = os.environ.get("CHARACTER_EVIDENCE_CALLBACK_URL", "").strip()
    signing_key = os.environ.get("CHARACTER_EVIDENCE_CALLBACK_SIGNING_KEY", "")
    if not callback_url.startswith("https://") or not signing_key:
        raise RuntimeError("signed Character Evidence callback is not configured")
    raw = envelope.model_dump_json().encode("utf-8")
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
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"BestShiny callback failed with HTTP {response.status_code}")


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


__all__ = ["create_api", "deliver_callback", "failure_envelope"]
