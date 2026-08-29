"""Drive one real, smallest-approved generation through the whole loop.

This is the closed loop the platform is judged on, and every stage of it is a
place where a green offline suite can still be wrong:

    quote → credits reserve → provider → poll → OSS → finalize → debit

Each stage is checked against the thing it claims, not against the stage before
it: the quote against `POST /api/pricing/estimate`, the reservation against
`workspace_credit_entries`, the transfer against a `HEAD` on the real bucket,
and the debit against the append-only `workspace_credit_events` trail. A run
that ends `COMPLETED` with no settled credit entry is a failure here even
though the picture came back.

It submits through `POST /api/passenger/generate` — the endpoint the Create
canvas itself uses — so what is proven is the path a paying user takes, not a
convenient one. That is also why the image target names no model: image targets
are router-owned (QA-018), and the permit is minted for the model the router is
expected to pick, then checked against the one it actually picked.

    uv run python scripts/live_canary.py image                    # plan + cost
    uv run python scripts/live_canary.py image --confirm-spend    # billed
    uv run python scripts/live_canary.py video --confirm-spend    # billed
    uv run python scripts/live_canary.py video --failure-drill    # free

`--failure-drill` submits without minting a permit. The request is priced and
the credits are reserved, and then the live gate refuses it with
`LIVE_CANARY_DENIED` and `submission_state=NOT_SENT` — nothing reaches the
provider, nothing is billed. It exists to prove the other half of the loop:
that a refusal releases the reservation instead of stranding it, and that the
failure arrives as a mapped `error_code` rather than a stack trace.

Money is only spent with `--confirm-spend`, and only inside a `LiveCanaryPermit`
whose request and cost ceilings this script mints first. Prints no secret.

Operator inputs, exported into the shell (never committed, never echoed):

    CANARY_ACCESS_TOKEN   bearer token for a workspace user that holds credits
    CANARY_PROJECT_ID     a project that user may write to

`scripts/canary_session.py` resolves both from the local QA account:

    eval "$(uv run python scripts/canary_session.py --export)"

`PLATFORM_API_KEY`, `DATABASE_URL` and the `S3_*` settings are read from the
same `Settings` the application uses.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_registry_core.live_canary import (  # noqa: E402
    CanaryLoop,
    record_canary_outcome,
)
from platform_database import Database  # noqa: E402
from platform_shared import S3CompatibleStorage, Settings  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

OK, FAIL, WARN = "ok  ", "FAIL", "WARN"

# The worker deliberately never processes this state again; it is terminal for
# an unattended canary even though an operator may later reconcile it.
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "REJECTED", "WORKER_NEEDS_USER_ACTION"}

# Every permit this script has ever minted counts against this. One model at a
# time is the rule, but a rule that is only in a runbook does not hold a budget:
# a typo in a per-model ceiling, or a model run twice, is caught here instead.
GLOBAL_CANARY_COST_CEILING_USD = Decimal("10")

# The listing endpoint's hard cap. Asking for exactly it turns "there are more
# than this" into something the caller can see rather than silently under-count.
_PERMIT_PAGE = 100


@dataclass(frozen=True)
class Target:
    """One canary target, with the ceiling the operator authorized for it."""

    name: str
    # What the permit is minted for, and what the resolved job must match.
    provider: str
    model: str
    max_requests: int
    max_cost_usd: str
    permit_hours: int
    # The smallest request this path will accept. Anything larger is a bigger
    # bill for the same evidence.
    body: dict[str, Any]
    quote: dict[str, Any]


TARGETS = {
    "image": Target(
        name="image",
        provider="openrouter",
        model="openai/gpt-image-2",
        max_requests=1,
        # 0.00588 USD published minimum (196 output tokens at quality=low),
        # rounded up with a small margin for the prompt text tokens.
        max_cost_usd="0.05",
        permit_hours=2,
        body={
            "media_type": "image",
            # Empty on purpose. An image request that names a model is refused;
            # the creative task is the whole request and the router resolves it.
            "provider": "",
            "model": "",
            "image_task": "auto",
            "prompt": "a single paper lantern on a wet street at night",
            "aspect_ratio": "1:1",
            "resolution": "720p",
        },
        quote={"media_type": "image", "resolution": "720p", "reference_count": 0},
    ),
    "video": Target(
        name="video",
        provider="seedance",
        # Ark's published ID. `seedance-2.5` was this platform's logical name
        # leaking into the field that names an execution target.
        model="doubao-seedance-2-5-260628",
        max_requests=1,
        # The live Ark endpoint rejected the model card's documented 4s floor
        # on 2026-08-29. Five seconds is the next-smallest integer request and
        # costs about USD 1.1145 at the published 720p estimate.
        max_cost_usd="1.25",
        permit_hours=2,
        body={
            "media_type": "video",
            "provider": "seedance",
            "model": "doubao-seedance-2-5-260628",
            "prompt": "a paper lantern drifting upward over a wet street at night",
            "aspect_ratio": "9:16",
            "resolution": "720p",
            # Four seconds is still admitted by the registry so existing calls
            # fail closed with Ark's own response, but the deployment canary
            # uses the smallest duration this account has not rejected.
            "duration": 5,
        },
        quote={"media_type": "video", "duration": 5, "resolution": "720p", "reference_count": 0},
    ),
}


def _video_target(
    name: str,
    *,
    provider: str,
    model: str,
    duration: int,
    resolution: str,
    max_cost_usd: str,
    prompt: str = "a paper lantern drifting upward over a wet street at night",
) -> Target:
    """One video model at the smallest request its own capability profile allows.

    Duration and resolution are not stylistic here. They are the two axes every
    one of these providers prices on, so the smallest admissible pair is the
    cheapest possible proof that the wire format, the auth and the poll parsing
    are right. Anything larger is a bigger bill for identical evidence.
    """

    return Target(
        name=name,
        provider=provider,
        model=model,
        max_requests=1,
        max_cost_usd=max_cost_usd,
        permit_hours=2,
        body={
            "media_type": "video",
            "provider": provider,
            "model": model,
            "prompt": prompt,
            "aspect_ratio": "16:9",
            "resolution": resolution,
            "duration": duration,
        },
        quote={
            "media_type": "video",
            "duration": duration,
            "resolution": resolution,
            "reference_count": 0,
        },
    )


# Phase two, cheapest first. Each ceiling is the published minimum for that
# request plus a margin for rounding — never a round number chosen for comfort,
# because the ceiling is what the global budget is charged for while the permit
# is live. Durations are each model's own `min_duration`; resolutions are the
# cheapest entry in its own `supported_resolutions`.
TARGETS.update(
    {
        # 1s at 480p, xAI's own floor, at OpenRouter's published 0.05 USD/s.
        "grok-imagine-video": _video_target(
            "grok-imagine-video",
            provider="openrouter",
            model="x-ai/grok-imagine-video",
            duration=1,
            resolution="480p",
            max_cost_usd="0.15",
        ),
        # Veo durations are the discrete set 4/6/8 — 4 is the floor, not 1.
        "veo-3.1-lite": _video_target(
            "veo-3.1-lite",
            provider="openrouter",
            model="google/veo-3.1-lite",
            duration=4,
            resolution="720p",
            max_cost_usd="0.40",
        ),
        "kling-3-standard": _video_target(
            "kling-3-standard",
            provider="openrouter",
            model="kwaivgi/kling-v3.0-std",
            duration=3,
            resolution="720p",
            max_cost_usd="0.70",
        ),
        "veo-3.1-fast": _video_target(
            "veo-3.1-fast",
            provider="openrouter",
            model="google/veo-3.1-fast",
            duration=4,
            resolution="720p",
            max_cost_usd="0.70",
        ),
        "kling-3-pro": _video_target(
            "kling-3-pro",
            provider="openrouter",
            model="kwaivgi/kling-v3.0-pro",
            duration=3,
            resolution="720p",
            max_cost_usd="0.90",
        ),
        # Already VERIFIED_LIVE on three clips; kept so the target set is the
        # whole registry and a re-verification after a contract change is one
        # command rather than a hand-built request.
        "wan-2.7": _video_target(
            "wan-2.7",
            provider="wan",
            model="wan-2.7",
            duration=2,
            resolution="720p",
            max_cost_usd="0.40",
        ),
        # 2s at 480p, the floor of OpenRouter's own published duration set
        # (2-30) at its cheapest resolution. USD 0.10 at list; the endpoint
        # currently carries a 15% discount, so the charge should come in at
        # 0.085 and the ceiling covers the list figure either way.
        "wan-3.0": _video_target(
            "wan-3.0",
            provider="openrouter",
            model="alibaba/wan-3.0",
            duration=2,
            resolution="480p",
            max_cost_usd="0.20",
        ),
        "seedance-2.5": TARGETS["video"],
        "veo-3.1": _video_target(
            "veo-3.1",
            provider="openrouter",
            model="google/veo-3.1",
            duration=4,
            resolution="720p",
            max_cost_usd="2.00",
        ),
    }
)


@dataclass
class Report:
    failures: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)

    def say(self, mark: str, label: str, detail: str = "") -> None:
        if mark == FAIL:
            self.failures += 1
        print(f"  [{mark}] {label:34} {detail}")

    def stage(self, title: str) -> None:
        print(f"\n=== {title} " + "=" * max(0, 74 - len(title)))


def _call(
    url: str,
    *,
    token: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> tuple[int, dict[str, Any]]:
    payload = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() or "{}"
        try:
            return int(exc.code), json.loads(raw)
        except json.JSONDecodeError:
            return int(exc.code), {"detail": raw[:500]}


def _row(engine, statement: str, parameters: dict[str, Any]) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    with engine.connect() as connection:
        found = connection.execute(text(statement), parameters).first()
    return dict(found._mapping) if found else None


def _credit_state(engine, job_id: str) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    return _row(
        engine,
        "select id, credits, status, settled_credits, refunded_credits, balance_after, "
        "reason, reconciliation_reason from workspace_credit_entries where generation_job_id = :job",
        {"job": job_id},
    )


def _media_asset(engine, asset_id: str) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    return _row(
        engine,
        "select id, storage_key, sha256, mime_type, size_bytes, provider, public_url "
        "from media_assets where id = :id",
        {"id": asset_id},
    )


def _credit_events(engine, job_id: str) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "select event_type, credits, balance_delta, balance_after, reason "
                "from workspace_credit_events where generation_job_id = :job "
                "order by created_at, id"
            ),
            {"job": job_id},
        ).all()
    return [dict(row._mapping) for row in rows]


def _report_events(engine, job_id: str, report: Report) -> None:  # type: ignore[no-untyped-def]
    events = _credit_events(engine, job_id)
    if not events:
        report.say(FAIL, "credit events", "no append-only trail for this job")
    for event in events:
        report.say(
            OK,
            f"  event {event['event_type']}",
            f"Δ{event['balance_delta']} → {event['balance_after']} · {event['reason']}",
        )
    report.evidence["credit_events"] = [event["event_type"] for event in events]


def _permit_exposure(permit: dict[str, Any]) -> Decimal:
    """What this permit can still cost, plus whatever it already drew.

    A permit that is still ACTIVE is worth its whole authorisation: nothing has
    stopped it from drawing the rest of it. A permit that is EXHAUSTED or
    EXPIRED can never draw again, so it is worth what it actually drew, plus
    anything still held against a usage nobody has reconciled — an UNCERTAIN
    usage might yet turn out to have been billed.

    Charging a dead permit its full authorisation for ever is the rule that
    would have ended this audit before it began: two refused attempts, USD 8 of
    ceilings, USD 0 billed, and every remaining model locked out of a budget
    none of them had spent.
    """

    authorised = Decimal(str(permit.get("max_cost_usd") or "0"))
    actual = Decimal(str(permit.get("actual_cost_usd") or "0"))
    held = Decimal(str(permit.get("reserved_cost_usd") or "0"))
    if str(permit.get("status")) == "ACTIVE":
        return max(authorised, actual + held)
    return actual + held


def _global_ceiling_remaining(settings: Settings, api: str, report: Report) -> Decimal | None:
    """Refuse to mint another permit once the audit as a whole has spent enough.

    Per-model ceilings bound one mistake. They do not bound a sequence of them,
    and a sequence is exactly what a model-by-model audit is.
    """

    status, body = _call(
        f"{api}/internal/live-canary-permits?limit={_PERMIT_PAGE}",
        token=settings.platform_api_key,
    )
    if status != 200:
        report.say(FAIL, "global ceiling", f"cannot read permits: HTTP {status}")
        return None
    # The endpoint answers `{"limit": n, "permits": [...]}`. Reading the wrong
    # key here did not raise: it defaulted to an empty list, so the ceiling
    # counted nothing and authorised everything.
    listed = body.get("permits")
    if not isinstance(listed, list):
        report.say(FAIL, "global ceiling", f"permit listing has no permits array: {sorted(body)}")
        return None
    if len(listed) >= _PERMIT_PAGE:
        # A ceiling computed from part of the history is not a ceiling.
        report.say(FAIL, "global ceiling", f"{len(listed)} permits fills the page; cannot total them")
        return None
    committed = sum((_permit_exposure(item) for item in listed if isinstance(item, dict)), Decimal("0"))
    drawn = sum(
        (Decimal(str(item.get("actual_cost_usd") or "0")) for item in listed if isinstance(item, dict)),
        Decimal("0"),
    )
    remaining = GLOBAL_CANARY_COST_CEILING_USD - committed
    report.say(
        OK if remaining > 0 else FAIL,
        "global ceiling",
        f"USD {committed} of {GLOBAL_CANARY_COST_CEILING_USD} exposed across {len(listed)} permit(s), "
        f"USD {drawn} actually drawn, USD {remaining} left",
    )
    return remaining if remaining > 0 else None


def _mint_permit(settings: Settings, target: Target, api: str, report: Report) -> dict[str, Any] | None:
    """Bound the spend before anything can spend it."""

    remaining = _global_ceiling_remaining(settings, api, report)
    if remaining is None:
        return None
    if Decimal(target.max_cost_usd) > remaining:
        report.say(
            FAIL,
            "permit refused",
            f"USD {target.max_cost_usd} would exceed the remaining USD {remaining}",
        )
        return None
    # Snapped to the hour, because the Idempotency-Key below is bucketed by hour
    # while the permit facts carry the exact expiry. With a to-the-microsecond
    # timestamp the two disagree: the key says "same request", the facts say
    # "different request", and a second run inside the same hour is refused 409
    # instead of replaying the permit it already minted. Snapping up keeps the
    # authorised window at least as long as asked for.
    requested = datetime.now(UTC) + timedelta(hours=target.permit_hours)
    expires_at = requested.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    status, body = _call(
        f"{api}/internal/live-canary-permits",
        token=settings.platform_api_key,
        method="POST",
        headers={
            "Idempotency-Key": (
                f"canary:{target.name}:{target.max_requests}:{target.max_cost_usd}"
                f":{expires_at:%Y%m%dT%H}"
            )
        },
        body={
            "provider": target.provider,
            "model": target.model,
            "max_requests": target.max_requests,
            "max_cost_usd": target.max_cost_usd,
            "expires_at": expires_at.isoformat(),
            "purpose": f"Production closed-loop canary for {target.provider}:{target.model}",
            "explicit_confirmation": True,
        },
    )
    if status not in {200, 201}:
        report.say(FAIL, "canary permit", f"HTTP {status} — {body.get('detail')}")
        return None
    report.say(
        OK,
        "canary permit",
        f"{body.get('max_requests')} requests / USD {body.get('max_cost_usd')} / "
        f"{target.permit_hours}h{' (replayed)' if body.get('replayed') else ''}",
    )
    return body


def _preflight(settings: Settings, report: Report) -> tuple[str, str] | None:
    token = os.environ.get("CANARY_ACCESS_TOKEN", "").strip()
    project_id = os.environ.get("CANARY_PROJECT_ID", "").strip()
    if not token or not project_id:
        report.say(FAIL, "operator inputs", "export CANARY_ACCESS_TOKEN and CANARY_PROJECT_ID")
        print(
            "\n  Both come from a real workspace user, which this script deliberately does\n"
            "  not create: sign in, open a project you can write to, and export that\n"
            "  session's bearer token and the project's id.\n"
        )
        return None
    if not settings.platform_api_key.strip():
        report.say(FAIL, "PLATFORM_API_KEY", "required to mint the canary permit")
        return None
    report.say(OK, "operator inputs", f"project {project_id}")
    for label, ready, detail in (
        ("PROVIDER_MODE", settings.provider_mode == "live", settings.provider_mode),
        ("ALLOW_LIVE_PROVIDER_CALLS", settings.allow_live_provider_calls, ""),
        ("LIVE_PROVIDER_CONFIRMATION", bool(settings.live_provider_confirmation.strip()), ""),
        ("S3 object storage", settings.storage_backend == "s3", settings.s3_bucket),
    ):
        report.say(OK if ready else FAIL, label, detail)
    if report.failures:
        print("\n  The live gate is incomplete. scripts/preflight_live.py explains each one.\n")
        return None
    return token, project_id


def _poll(
    api: str, token: str, job_id: str, args: argparse.Namespace, report: Report
) -> dict[str, Any] | None:
    deadline = time.monotonic() + args.poll_timeout
    seen: set[str] = set()
    while True:
        status, job = _call(f"{api}/v1/generations/{job_id}", token=token)
        if status != 200:
            report.say(FAIL, "poll", f"HTTP {status} — {job.get('detail')}")
            return None
        state = str(job.get("status"))
        if state not in seen:
            seen.add(state)
            report.say(
                OK,
                f"status → {state}",
                f"provider_job {job.get('provider_job_id') or '—'} · "
                f"submission {job.get('submission_state') or '—'}",
            )
        if state in TERMINAL:
            return job
        if time.monotonic() > deadline:
            report.say(FAIL, "poll", f"still {state} after {args.poll_timeout}s")
            return None
        time.sleep(args.poll_interval)


def _check_storage(settings: Settings, asset: dict[str, Any], report: Report) -> bool:
    storage = S3CompatibleStorage(
        bucket=settings.s3_bucket,
        cache_root=settings.storage_root,
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
        public_base_url=settings.public_base_url,
        addressing_style=settings.s3_addressing_style,
        enforce_checksum=settings.s3_enforce_upload_checksum,
    )
    stat = storage.stat(asset["storage_key"])
    if stat is None:
        report.say(FAIL, "object in bucket", f"HEAD found nothing at {asset['storage_key']}")
        return False
    same = int(stat.size) == int(asset["size_bytes"])
    report.say(
        OK if same else FAIL,
        "object in bucket",
        f"{settings.s3_bucket}/{asset['storage_key']} · {stat.size} bytes"
        + ("" if same else f" (row says {asset['size_bytes']})"),
    )
    return same


def run(args: argparse.Namespace) -> int:
    settings = Settings()
    target = TARGETS[args.target]
    report = Report()
    api = args.api.rstrip("/")

    report.stage(f"Canary target — {target.provider} · {target.model}")
    inputs = _preflight(settings, report)
    if inputs is None:
        return 1
    token, project_id = inputs
    engine = create_engine(settings.database_url)

    # ---- 1. QUOTE -----------------------------------------------------------
    report.stage("1. Quote")
    status, quote = _call(
        f"{api}/api/pricing/estimate",
        token=token,
        method="POST",
        body={"provider": target.provider, "model": target.model, **target.quote},
    )
    if status != 200:
        report.say(FAIL, "pricing estimate", f"HTTP {status} — {quote.get('detail')}")
        return 1
    credits = int(quote["credits"])
    report.say(
        OK,
        "pricing estimate",
        f"{credits} CR ≈ USD {quote['estimated_total_usd']} "
        f"(provider USD {quote['provider_cost_usd']}, {quote['version']})",
    )
    report.evidence["quote"] = quote

    if args.failure_drill:
        report.say(WARN, "failure drill", "no permit will be minted — refused after reserve, free")
        permit = {}
    elif not args.confirm_spend:
        print(
            f"\n  Plan only. This would spend real money at {target.provider}: about USD "
            f"{quote['estimated_total_usd']} of provider cost,\n  reserved as {credits} credits "
            f"against the workspace, inside a {target.max_requests}-request / USD "
            f"{target.max_cost_usd} / {target.permit_hours}h permit.\n\n"
            "  Re-run with --confirm-spend to execute it.\n"
        )
        return 0
    else:
        minted = _mint_permit(settings, target, api, report)
        if minted is None:
            return 1
        permit = minted
        report.evidence["permit_id"] = permit.get("id")

    # ---- 2. RESERVE ---------------------------------------------------------
    report.stage("2. Credits reserved")
    started = time.monotonic()
    status, job = _call(
        f"{api}/api/passenger/generate",
        token=token,
        method="POST",
        body={
            "project_id": project_id,
            "idempotency_key": f"canary-{target.name}-{uuid.uuid4()}",
            **target.body,
        },
    )
    if status == 402:
        report.say(FAIL, "submit", f"HTTP 402 — {job.get('detail')} (top the workspace up first)")
        return 1
    if status not in {200, 202}:
        report.say(FAIL, "submit", f"HTTP {status} — {job.get('detail')}")
        return 1
    job_id = job["id"]
    report.evidence["job_id"] = job_id
    resolved = f"{job.get('provider')} · {job.get('model')}"
    report.say(OK, "submitted", f"job {job_id} · {job.get('status')} · {job.get('credit_status')}")
    on_target = job.get("provider") == target.provider and job.get("model") == target.model
    report.say(
        OK if on_target else FAIL,
        "resolved target",
        resolved + ("" if on_target else f" — permit covers {target.provider} · {target.model}"),
    )
    quoted_here = int(job.get("estimated_credits") or 0)
    report.say(
        OK if quoted_here == credits else FAIL,
        "quote held through submit",
        f"{quoted_here} CR reserved against {credits} CR quoted",
    )

    entry = _credit_state(engine, job_id)
    if entry is None:
        report.say(FAIL, "reservation", "no workspace_credit_entries row for this job")
    else:
        held = entry["status"] == "RESERVED" and int(entry["credits"]) == credits
        report.say(
            OK if held else FAIL,
            "reservation",
            f"{entry['credits']} CR {entry['status']} · balance {entry['balance_after']}",
        )

    # ---- 3. PROVIDER + POLL -------------------------------------------------
    report.stage("3. Provider and poll")
    job = _poll(api, token, job_id, args, report)
    if job is None:
        return _finish(report)
    state = str(job.get("status"))
    elapsed = time.monotonic() - started
    report.evidence["elapsed_seconds"] = round(elapsed, 1)
    report.say(OK, "terminal state", f"{state} in {elapsed:.1f}s · {job.get('attempt_count')} attempt(s)")

    if state != "COMPLETED":
        return _report_failure_path(
            engine, job, job_id, report, drill=args.failure_drill, settings=settings, target=target
        )

    # ---- 4. OSS -------------------------------------------------------------
    report.stage("4. Object storage")
    asset_id = job.get("output_asset_id")
    if not asset_id:
        report.say(FAIL, "output asset", "job completed with no output_asset_id")
        return _finish(report)
    asset = _media_asset(engine, asset_id)
    if asset is None:
        report.say(FAIL, "output asset", f"media_assets row {asset_id} missing")
        return _finish(report)
    report.evidence["asset"] = {
        "id": asset["id"],
        "storage_key": asset["storage_key"],
        "sha256": asset["sha256"],
        "size_bytes": asset["size_bytes"],
        "mime_type": asset["mime_type"],
    }
    report.say(OK, "media asset", f"{asset['mime_type']} · {asset['size_bytes']} bytes")
    in_bucket = _check_storage(settings, asset, report)

    # ---- 5. FINALIZE + DEBIT ------------------------------------------------
    report.stage("5. Finalize and debit")
    settled = _credit_state(engine, job_id)
    if settled is None:
        report.say(FAIL, "settlement", "reservation row disappeared")
    else:
        done = settled["status"] == "SETTLED" and int(settled["settled_credits"]) == int(settled["credits"])
        report.say(
            OK if done else FAIL,
            "settlement",
            f"{settled['status']} · settled {settled['settled_credits']}/{settled['credits']} CR "
            f"· balance {settled['balance_after']}",
        )
        report.evidence["credits"] = {
            "reserved": int(settled["credits"]),
            "settled": int(settled["settled_credits"]),
            "refunded": int(settled["refunded_credits"]),
            "status": settled["status"],
        }
    _report_events(engine, job_id, report)
    _stamp(
        settings,
        report,
        CanaryLoop(
            provider=target.provider,
            model=target.model,
            job_id=job_id,
            submission_state=str(job.get("submission_state") or ""),
            terminal_status=state,
            output_asset_id=asset_id,
            artifact_bytes=int(asset["size_bytes"] or 0),
            artifact_in_storage=in_bucket,
            credit_status=str(settled["status"]) if settled else "",
            credits_reserved=int(settled["credits"]) if settled else 0,
            credits_settled=int(settled["settled_credits"]) if settled else 0,
            provider_task_id=job.get("provider_job_id"),
        ),
    )
    return _finish(report)


def _stamp(settings: Settings, report: Report, loop: CanaryLoop) -> None:
    """Write what this run earned, and say so in the report.

    A run that earns nothing says so out loud. Silence here used to be the whole
    problem: the sweep ran, the loop closed, and every model still read NOT_RUN.
    """

    record = record_canary_outcome(Database(settings.database_url), loop)
    if record is None:
        report.say(
            WARN,
            "live_canary_status",
            "unchanged — this run earned no verdict about the model",
        )
        return
    report.say(
        OK,
        "live_canary_status",
        f"{record.logical_name}: {record.previous_status} -> {record.status}",
    )
    report.evidence["live_canary_status"] = {
        "logical_name": record.logical_name,
        "previous": record.previous_status,
        "status": record.status,
        "detail": record.detail,
        "observed_at": record.observed_at,
    }


def _report_failure_path(  # type: ignore[no-untyped-def]
    engine,
    job: dict[str, Any],
    job_id: str,
    report: Report,
    *,
    drill: bool,
    settings: Settings,
    target: Target,
) -> int:
    """The half that matters when nothing works: mapped error, released credits."""

    report.stage("Error mapping and credit release")
    submission = str(job.get("submission_state") or "")
    expected_drill = (
        drill
        and job.get("error_code") == "LIVE_CANARY_DENIED"
        and submission == "NOT_SENT"
    )
    if drill:
        report.say(
            OK if expected_drill else FAIL,
            "failure drill outcome",
            (
                "live gate refused before provider submission"
                if expected_drill
                else f"expected LIVE_CANARY_DENIED/NOT_SENT, got "
                f"{job.get('error_code')}/{submission or '—'}"
            ),
        )
    else:
        # A safely mapped failure is useful evidence, but it is not a successful
        # production canary. Without this explicit failure the report ended in
        # "Closed loop verified" after an INVALID_REQUEST and no output asset.
        report.say(
            FAIL,
            "canary outcome",
            f"expected COMPLETED, got {job.get('status') or '—'}",
        )
    report.say(
        OK if job.get("error_code") else FAIL,
        "mapped error_code",
        f"{job.get('error_code') or 'MISSING'} — {job.get('error_message') or ''}",
    )
    report.say(
        OK,
        "submission_state",
        f"{submission or '—'}" + ("  (nothing reached the provider)" if submission == "NOT_SENT" else ""),
    )
    report.say(OK, "safe_to_retry", str(job.get("safe_to_retry")))
    report.evidence["failure"] = {
        "error_code": job.get("error_code"),
        "submission_state": submission,
        "safe_to_retry": job.get("safe_to_retry"),
    }

    released = _credit_state(engine, job_id)
    if released is None:
        report.say(FAIL, "credit release", "reservation row disappeared")
    else:
        freed = released["status"] == "REFUNDED"
        needs_operator = released["status"] == "RECONCILIATION_REQUIRED"
        report.say(
            OK if freed else (WARN if needs_operator else FAIL),
            "credit release",
            f"{released['status']} · refunded {released['refunded_credits']}/{released['credits']} CR "
            f"· balance {released['balance_after']}"
            + (f" · {released['reconciliation_reason']}" if needs_operator else ""),
        )
        report.evidence["credits"] = {
            "reserved": int(released["credits"]),
            "settled": int(released["settled_credits"]),
            "refunded": int(released["refunded_credits"]),
            "status": released["status"],
        }
    _report_events(engine, job_id, report)

    _stamp(
        settings,
        report,
        CanaryLoop(
            provider=target.provider,
            model=target.model,
            job_id=job_id,
            submission_state=submission,
            terminal_status=str(job.get("status") or ""),
            credit_status=str(released["status"]) if released else "",
            credits_reserved=int(released["credits"]) if released else 0,
            credits_settled=int(released["settled_credits"]) if released else 0,
            error_code=job.get("error_code"),
            provider_task_id=job.get("provider_job_id"),
        ),
    )

    if expected_drill:
        print(
            "\n  The drill did what it is for: priced, reserved, refused at the live gate\n"
            "  before a socket opened, and the reservation came back. No provider money.\n"
        )
    return _finish(report)


def _finish(report: Report) -> int:
    report.stage("Evidence")
    print(json.dumps(report.evidence, indent=2, default=str))
    print()
    if report.failures:
        print(f"  {report.failures} stage(s) failed.\n")
        return 1
    print("  Closed loop verified end to end.\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=sorted(TARGETS))
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="actually run the billed request; without it this prints the plan and its cost",
    )
    parser.add_argument(
        "--failure-drill",
        action="store_true",
        help="reserve, then let the live gate refuse it — proves credit release, costs nothing",
    )
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--poll-timeout", type=int, default=600)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
