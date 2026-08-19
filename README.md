# AI Video Production Platform V1

A provider-neutral AI video generation platform built as a modular monolith plus a Chrome browser worker.
Google Flow is the first implemented `GenerationProvider`; production, media, jobs, agents and continuity do not
import Flow-specific clients.

## What runs now

- `Project → Episode → Scene → Shot` production domain.
- PostgreSQL in Docker and SQLite for local development, with Alembic migration.
- Content-addressed `MediaAsset` registry and local storage provider.
- Per-provider/per-account media ID bindings; the same character reference is uploaded once and reused.
- Persistent generation jobs, event history, payload-hash idempotency and `409` conflict protection.
- Load-aware account selection using provider, capability, model, credits, tier, inflight, pending, worker capacity,
  cooldown and error rate.
- Database-backed browser command protocol over HTTP polling and WebSocket.
- Google Flow text-to-video, start-frame, start/end-frame, reference-to-video, image generation, upload, poll,
  credits and signed media URL mapping.
- Safe paid-call behavior: uncertain submissions are not automatically generated again. Late browser responses can
  be reconciled into the original job.
- Shot end-frame extraction at `duration - safe_offset`, a quality-detector interface and automatic propagation to
  the following shot.
- Provider stubs for Seedance, official Veo, Grok and Omni.
- OpenAI-compatible image/video request adapters that translate into the internal `GenerationRequest`.
- File-based skills and an Agent runtime that can access only Production Engine, Generation Gateway and Media
  Registry—not provider clients.

The source audit and exact migration map are in [docs/source-audit.md](docs/source-audit.md).

## Local startup

Requirements: Python 3.12+, `uv`, FFmpeg and Chrome.

```bash
cp .env.example .env
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn video_platform_api.main:app --reload --port 8080
```

In a second terminal:

```bash
uv run video-platform-worker
```

API documentation: <http://127.0.0.1:8080/docs>.

SQLite is the default. For a production-like local stack:

```bash
docker compose up --build
```

The compose stack contains `api`, `worker` and `postgres`; no unnecessary Redis dependency is used.

## Browser worker setup

1. Open `chrome://extensions`, enable Developer mode and choose “Load unpacked”.
2. Select `apps/browser-worker-extension/`.
3. Create a Google Flow provider account through `POST /v1/accounts`; enter the returned account ID in the
   extension popup.
4. Open Google Flow in Chrome and sign in normally. The extension passively observes the browser's Flow bearer
   token and public API-key query parameter.
5. Before a generation that requires reCAPTCHA, click “Authorize next generation” in the extension. This is a
   deliberate user action and is valid for one request only.

The extension does not bypass CAPTCHA, interactive verification, platform access controls or risk controls. If
Google requires interaction, the worker reports `NEEDS_USER_ACTION`; the job pauses without issuing another paid
generation.

## End-to-end API flow

Create the production hierarchy:

```bash
curl -s http://localhost:8080/v1/projects \
  -H 'Content-Type: application/json' \
  -d '{"title":"Vertical Drama"}'

curl -s http://localhost:8080/v1/episodes \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"PROJECT_ID","title":"Pilot","episode_number":1}'

curl -s http://localhost:8080/v1/scenes \
  -H 'Content-Type: application/json' \
  -d '{"episode_id":"EPISODE_ID","sequence":1,"description":"A rain-soaked station"}'

curl -s http://localhost:8080/v1/shots \
  -H 'Content-Type: application/json' \
  -d '{"scene_id":"SCENE_ID","sequence":1,"duration":8,"prompt":"The woman turns toward the train; one visible action.","provider":"google_flow","model":"veo"}'
```

Create a Flow account and upload a character reference:

```bash
curl -s http://localhost:8080/v1/accounts \
  -H 'Content-Type: application/json' \
  -d '{"provider":"google_flow","account_identifier":"flow-user","tier":"PRO","credits":100,"video_capacity":2,"supported_models":["veo"],"provider_project_id":"FLOW_PROJECT_ID"}'

curl -s http://localhost:8080/v1/assets \
  -F project_id=PROJECT_ID \
  -F asset_type=CHARACTER_REFERENCE \
  -F file=@character.png
```

Submit Shot 1 with an idempotency key:

```bash
curl -s http://localhost:8080/v1/generations \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id":"PROJECT_ID",
    "shot_id":"SHOT_1_ID",
    "type":"video",
    "provider":"google_flow",
    "model":"veo",
    "prompt":"The woman turns toward the train; one visible action.",
    "duration":8,
    "aspect_ratio":"9:16",
    "reference_asset_ids":["CHARACTER_ASSET_ID"],
    "idempotency_key":"ep01-shot001-v1"
  }'
```

The worker reserves an eligible account, reuses or uploads media, submits Flow, persists the provider media ID,
polls, downloads the output, creates a `VIDEO` asset, extracts an `END_FRAME`, and assigns it to a following shot
whose `continuity_mode` is `PREVIOUS_END_FRAME`.

Submitting the same key and same payload returns the original job. The same key with a different payload returns
`409 Conflict`.

## Required API

- `POST /v1/generations`
- `GET /v1/generations/{id}`
- `POST /v1/generations/{id}/retry`
- `POST /v1/generations/{id}/reconcile`
- `POST /v1/assets`
- `GET /v1/assets/{id}`
- `GET /v1/providers`
- `GET /v1/providers/{provider}/health`
- `GET /v1/accounts`
- `GET /v1/workers`
- `GET /health`
- `POST /v1/images/generations` and `POST /v1/videos/generations` compatibility adapters

## Generation lifecycle and events

Jobs progress through `NEW → QUEUED → SUBMITTED → RUNNING → COMPLETED`. Recoverable errors use `RETRY_WAIT`;
unsafe or user-interactive states use `WORKER_NEEDS_USER_ACTION`; permanent failures use `FAILED`.

Events include `JOB_CREATED`, `ACCOUNT_SELECTED`, `WORKER_SELECTED`, `ASSET_RESOLVED`, `ASSET_UPLOADED`,
`REQUEST_SUBMITTED`, `PROVIDER_JOB_STARTED`, `PROVIDER_JOB_POLL`, `PROVIDER_JOB_COMPLETED`, `MEDIA_DOWNLOADED`,
`END_FRAME_EXTRACTED`, `JOB_COMPLETED`, and categorized errors. `ORPHAN_RESPONSE_RECOVERED` records a late browser
response attached to its original job.

## Tests and quality gates

```bash
uv run pytest -q
uv run ruff format --check . --exclude references
uv run ruff check . --exclude references
uv run mypy
docker compose config -q
```

The regression suite covers account scheduling, concurrency release, media deduplication, provider media reuse,
idempotency conflict/replay, uncertain paid-call retry, worker disconnect/reconnect, stale connection rejection,
restart recovery, late-response reconciliation, provider routing, Flow request mapping, end-frame extraction and
Shot 1 → Shot 2 continuity.

## Real V1 limitations

- Google Flow uses private, changeable web protocols. Endpoint or model keys can change without notice and may
  require adapter updates.
- A real end-to-end Flow generation requires the user's eligible signed-in Google account, Flow project and a
  user-authorized request. Automated tests do not spend provider credits.
- Google Flow cancellation is not exposed reliably; local cancellation stops future platform work but cannot
  guarantee cancellation of an already submitted remote job.
- The default end-frame quality detector rejects only missing/tiny frames. A learned black-frame, blur and face
  quality detector remains an extension point.
- Local storage is implemented; S3, R2 and OSS implement the same `StorageProvider` contract but are not included in
  V1.
- Seedance, official Veo, Grok and Omni are registered provider slots with honest `not configured` health results,
  not fake implementations.
- The separate dashboard frontend is not built in V1; operations are available through the typed API and OpenAPI
  UI.

