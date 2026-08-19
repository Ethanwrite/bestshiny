# Source repository audit

Audit date: 2026-08-19. Repositories are kept under `references/` for engineering review and are excluded
from this repository's commits.

| Repository | Audited commit | License result | Runtime dependency |
| --- | --- | --- | --- |
| `TheSmallHanCat/flow2api` | `193008716e3cc57d6c22be418e134e7eabf84358` | MIT, © 2025 TheSmallHanCat | No |
| `crisng95/flowkit` | `66e859645fd14bd33f6ceb9ac143a3ff896c61d8` | MIT, © 2026 tuannguyenhoangit-droid | No |
| `kodelyx/flow-agent` | `113f17e7057a3195808edb71b5c4c3b6e234163d` | No license file found | No; behavior reference only |

Because `flow-agent` contains no license grant, none of its source was copied. Its observable persistence and
recovery behaviors were independently implemented with SQLAlchemy records and our own worker protocol.

## Audit findings

### flow2api

- Google Flow calls: `src/services/flow_client.py`; generation routing/polling in
  `src/services/generation_handler.py`.
- Account/token management: `src/services/token_manager.py`, `src/core/models.py`, and account tier helpers in
  `src/core/account_tiers.py`.
- Load and concurrency: `src/services/load_balancer.py` combines tier/model filtering, inflight load, pending load,
  remaining capacity and round-robin fallback. `src/services/concurrency_manager.py` implements atomic slot acquire,
  release and bounded waits.
- Proxies: `src/services/proxy_manager.py`; proxy scope is kept outside our V1 because the browser worker owns the
  signed-in network context. `ProviderAccount.proxy_id` is reserved for a later transport implementation.
- Database: `src/core/database.py` persists tokens, Flow projects, tasks, request logs, statistics and configuration
  in SQLite. The domain is Flow-centric and was not copied.
- API routes: `src/api/routes.py` provides OpenAI-style request adapters; `src/api/admin.py` exposes account and
  operational administration.
- Health/monitoring: `src/core/monitoring.py`, `src/core/browser_runtime_status.py`.
- Chrome extension: `extension/background.js`, `extension/content.js`. It is CAPTCHA-token focused rather than a
  general generation worker and therefore was not used as the V1 protocol.
- Media: file caching exists in `src/services/file_cache.py`, but there is no provider-neutral `MediaAsset` domain.
- Idempotency: no durable, payload-hash business idempotency equivalent to V1 was found.

What was migrated: the load-aware candidate filtering, explicit slot reservation/release, tier/model eligibility,
credits/error-aware ordering and normalized generation error concepts were reworked into `AccountScheduler`,
`RetryPolicy` and persistent generation events. Flow request shapes remain isolated in `GoogleFlowProvider`.

### flowkit

- Domain/database: `agent/db/schema.py`, `agent/db/crud.py`, and `agent/models/` implement
  `Project → Video → Scene`, characters, requests and media IDs. V1 replaces this with
  `Project → Episode → Scene → Shot`.
- SDK/repository: `agent/sdk/repository.py` supplies a useful service-facing repository pattern.
- Flow transport: `agent/services/flow_client.py` and `extension/background.js` communicate through a browser
  bridge and handle signed-in Flow requests.
- Worker: `agent/worker/processor.py` handles prerequisites, dispatch, failure parsing, stale processing recovery
  and media re-upload.
- Chaining: `agent/services/scene_chain.py` creates continuation scenes but equates prior video media IDs with the
  next input. V1 deliberately creates a distinct `END_FRAME` asset.
- Media/project API: `agent/api/projects.py`, `scenes.py`, `videos.py`, `requests.py`, and `materials.py`.
- Extension: `extension/manifest.json`, `background.js`, `content.js`, `injected.js`, popup and side panel. It is MIT
  and is the structural basis for V1's browser transport.
- Skills: `skills/` and `.claude/commands/` demonstrate file-based operational skills.
- Polling/recovery: `agent/worker/processor.py` resets stale processing requests and recovers invalid remote entity
  references by re-uploading known media.

What was migrated: the MIT extension's browser-context request transport, passive credential capture, worker
processor boundaries, media re-upload concept and file-based skills structure. V1 changes the protocol to
`worker.register`, `worker.heartbeat`, `provider.request`, `provider.response`, `provider.error`, `asset.upload`,
`job.submit` and `job.poll`. Interactive authorization is explicitly user-triggered.

### flow-agent reference implementation

- Bridge: `flow-agent/flow_engine/bridge.py` and `http_bridge.py` contain WebSocket and HTTP-poll transports,
  multiple client selection, heartbeat/session expiry and orphan response handling.
- Persistent idempotency: `flow-agent/flow_server/idempotency.py` atomically stores key fingerprints and replay,
  conflict, processing, success and failure states.
- Job results: `flow-agent/flow_server/jobs.py` uses atomic JSON replacement for restart persistence.
- Media registry: `flow-agent/flow_server/media_history.py`, `history.py` and `flow_engine/media_store.py` use SHA-256,
  persistent media IDs, remote revalidation, stale-ID invalidation and local-byte recovery.
- Generation/polling: `flow-agent/flow_server/routes/generation.py` creates durable video jobs and carefully avoids
  a second paid call after an uncertain timeout. Generator functions are in `flow_engine/generators/`.
- Worker registry/round robin: `flow_engine/bridge.py` tracks client state and chooses among connected extensions.
- Extension: `flow-extension/` implements connection, command dispatch, token readiness and media URL resolution.
- Batch/OpenAI API: `flow_server/batch.py`, `routes/generation.py`, `routes/media.py`, `routes/system.py`.

Behavior independently implemented: database-backed idempotency, canonical payload hashes, persistent worker
commands, late-response reconciliation, submission states, restart recovery, multiple-worker registration,
heartbeat expiry, SHA-256 provider bindings, stale media validation and non-duplicating retry rules.

## Target mapping

| Required capability | V1 implementation |
| --- | --- |
| Production domain | `packages/domain/production_domain/models.py` |
| PostgreSQL/SQLite | `packages/database/platform_database/session.py`, Alembic migration `0001` |
| Media registry | `services/media-service/media_service/registry.py` |
| Storage abstraction | `packages/shared/platform_shared/storage.py` |
| Provider interface/router | `packages/provider-sdk/provider_sdk/`, `generation_gateway/providers.py` |
| Google Flow isolation | `providers/google-flow/google_flow_provider/` |
| Account scheduler/concurrency | `generation_gateway/scheduler.py` |
| Durable idempotency/jobs/events | `generation_gateway/gateway.py`, SQLAlchemy models |
| Browser worker runtime | `services/browser-runtime/`, persistent `worker_commands` |
| Chrome extension | `apps/browser-worker-extension/` |
| Shot continuity/end frame | `production_engine/continuity.py` |
| OpenAI adapters | `/v1/images/generations`, `/v1/videos/generations` in API app |
| Agent/skills boundary | `agents/runtime/`, `skills/*/SKILL.md` |

