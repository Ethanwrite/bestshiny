# Canvas workflows and workspace connections

Branch `claude/canvas-workflows-byok`, 2026-09-14. Alembic head moves to
`0083_canvas_workflows_user_connections` (`REQUIRED_SCHEMA_REVISION` moves with it).

Two product changes that meet at the generation gateway:

1. **Workspace connections ("bring your own API").** A workspace owner or admin adds the
   workspace's own API key for a language-model or video/image provider. Canvas nodes run on it,
   and the provider bills that account directly - not BestShiny credits.
2. **The canvas.** `/app` is now a node editor: users drag nodes onto an infinite canvas, connect
   typed ports and run the graph. Runs execute durably in the worker. The previous workbench
   (Create / Develop / Direct / Productions) is unchanged at `/app/studio`.

## 1. What a connection is

`user_connections` (one row per connection, workspace-owned):

| Column | Meaning |
| --- | --- |
| `protocol` | The wire format, which decides the adapter (§1.1) |
| `base_url` | Canonicalized origin + path prefix; HTTPS, no credentials/query/fragment |
| `capabilities_json` | The subset of the protocol's `chat` / `image` / `video` the workspace enabled |
| `models_json` | `[{id, capability}]` the workspace's members pick from (free typing is also allowed) |
| `secret_ciphertext` | Fernet (`CredentialVault`) of `"<row id>:<key>"`, so ciphertext moved to another row does not decrypt as that row's key; erased on delete |
| `secret_hint` | `sk-…9f2a`, the only form of the key any route returns |
| `status` | `ACTIVE`, `INVALID` (the last check was refused; still usable), `DELETED` (soft; the row is the anchor jobs point at) |

Routes (`apps/api/video_platform_api/canvas_routes.py`): any member lists connections and uses them
on a canvas; OWNER/ADMIN add, check (test, remote models), change and delete them.

| Method | Path |
| --- | --- |
| GET | `/v1/connections/protocols` |
| GET, POST | `/v1/workspaces/{workspace_id}/connections` (list carries `can_manage`) |
| PATCH, DELETE | `/v1/workspaces/{workspace_id}/connections/{id}` (an empty or omitted `api_key` keeps the key) |
| POST | `/v1/workspaces/{workspace_id}/connections/{id}/test` |
| GET | `/v1/workspaces/{workspace_id}/connections/{id}/remote-models` |

A connection of another workspace answers `404` through the caller's own workspace path and `403`
through the owner's. A validation error never echoes the submitted key: `api_key` has no pydantic
constraint (a length error would put it in the 422 body); the service validates it.

### 1.1 Protocols

`core/connections/connection_core/protocols.py`. A protocol is a wire format, not a vendor.

| Protocol | Chat | Image | Video | Implementation |
| --- | --- | --- | --- | --- |
| `openai_compatible` | yes | | | `POST /chat/completions`, `GET /models`; presets for OpenAI, DeepSeek, Moonshot, Zhipu, SiliconFlow, Qwen and Doubao compatible modes, Gemini's OpenAI endpoint, xAI, Groq, Mistral |
| `anthropic` | yes | | | Messages API: `POST /v1/messages`, `x-api-key`, `anthropic-version: 2023-06-01`, paginated `GET /v1/models` |
| `openrouter` | yes | yes | yes | chat as above; image/video through the platform's reviewed `OpenRouterProvider` |
| `volcengine_ark` | yes | yes | yes | chat as above; Seedream/Seedance through the reviewed `ArkProvider` |
| `alibaba_dashscope` | yes | | yes | chat on `{base}/compatible-mode/v1`; Wan video on `{base}/api/v1` through the reviewed `WanProvider` (Wan 2.7 media-array API), the connection's model id passed through as chosen |
| `mock` | yes | yes | yes | network-free echo, Pillow placeholder images, FFmpeg placeholder clips; **development and test only** |

Video and image generation are offered only where this repository already has an adapter written
against the vendor's own API reference, so a connection reuses that reviewed request builder with
the workspace's key rather than a second, unreviewed one. Suggested model ids are the ids the
platform registry already runs, or the ids the Anthropic API reference names.

Chat details worth knowing:

- Optional sampling parameters are sent only when the node sets them. A `400` that names the
  parameter is retried once without it (`temperature`, which current Claude models refuse), or with
  `max_completion_tokens` in place of `max_tokens` (OpenAI's newer models). Nothing else is retried.
- **Checking a key** (`/test`) lists the provider's models where that listing requires the key
  (OpenAI-compatible, Anthropic). Only the provider's verdict on the key - success, `401` or `403` -
  moves `status` (`ACTIVE` / `INVALID`); an unreachable host or an address the fence refuses is
  reported and changes nothing. OpenRouter's model list is public, so its check says the key is
  verified on first use (`listing_checks_key = false`); Ark and DashScope have no reviewed key-check
  endpoint and say the same without a request.
- Anthropic defaults `max_tokens` to 16,000 because adaptive thinking on current models counts
  against it. On `api.anthropic.com` for `claude-opus-5` and `claude-fable-5-1` the request opts
  into server-side refusal fallbacks (`anthropic-beta: server-side-fallback-2026-07-01`,
  `fallbacks: "default"`); the served model is recorded as `usage.served_model`. A gateway base URL
  gets neither. `stop_reason: "refusal"` fails the node as `MODEL_REFUSED`.
- Raw HTTP rather than the Anthropic SDK: every connection must pass the connect-time egress fence
  in this package's transport (§1.2).

### 1.2 Egress: the SSRF fence for user-supplied URLs

`core/connections/connection_core/egress.py`. A connection's base URL is typed by a user, so every
request made on its behalf is a request into whatever network that URL resolves to.

- **Where:** `FencedNetworkBackend` is the httpcore network backend of every connection client. It
  resolves the host itself, refuses the connection unless *every* address is public (`is_global`,
  so loopback, RFC 1918, link-local cloud metadata, carrier-grade NAT - including Alibaba's
  `100.100.100.200` - and IPv4-mapped forms are all refused), and dials the address it validated.
  TLS still verifies the certificate against the hostname. There is no window between the check
  and the connect for DNS rebinding to use.
- **No proxies** (`trust_env=False`), **no redirects** (a 3xx is reported as an error naming the
  final-URL fix), **bounded bodies** (`USER_CONNECTION_MAX_RESPONSE_BYTES`). An oversized body is
  reported `submitted=True` for a non-GET, because the request already reached the provider.
- `normalize_base_url` gives clear errors at save time; the backend is the security boundary.
- `FencedTransport` rebuilds httpx's connection pool to install the backend (httpx does not expose
  it); `tests/test_user_connections.py::test_a_real_client_never_reaches_loopback` pins that to the
  installed httpx release.
- `USER_CONNECTION_ALLOW_PRIVATE_NETWORK=true` (local Ollama/vLLM) is refused at startup in
  production.

## 2. A connection job in the gateway

A generation on a connection is an ordinary `generation_jobs` row with `provider = "byok"` and
`connection_id` set. It runs through `GenerationGateway` exactly like a platform job - durable
submission boundary, claims, polling, media validation and storage, Productions, cancellation,
restart recovery - with these differences, and only these:

| Platform job | Connection job |
| --- | --- |
| `providers.validate_target` and the persisted model switch | `UserConnectionResolver.generation_provider` builds the adapter with the connection's key; a deleted, foreign or capability-less connection is a `GenerationTargetError` before anything is sent |
| `WorkspaceCreditBalance.billable` decides the reservation | no reservation, `quoted_credits = 0`, `workspace_credit_required = false`; a caller-supplied quote is refused (`WorkspaceCreditConflict`) |
| production-budget authorization, canary permit, canary verdict | none of them: `_reserve_live_generation_fence`, `_require_live_generation_fence_boundary`, `_settle_live_generation_fence` and `_record_live_generation_canary_verdict` return early for `byok` - the job spends no platform money and is not evidence about a platform model |
| scheduler picks among the provider's accounts | the job is pinned (`select_account(account_id=...)`) to its connection's own capacity resource `byok://<connection id>` (`USER_CONNECTION_MAX_CONCURRENT_JOBS`), so it can never run on another workspace's key |
| model-switch `EXISTS` fence in the boundary transaction | connection-still-live `EXISTS` fence in the same transaction (`CONNECTION_UNAVAILABLE`) |
| artefacts fetched only from `PROVIDER_MEDIA_ALLOWED_HOSTS` | the provider's `media_fetch_policy` (`RemoteMediaFetchPolicy`) admits any public host with every other check unchanged (HTTPS on 443, no userinfo, public resolved and connected addresses, redirects re-validated per hop, byte cap); the OpenRouter key is presented only when the artefact's host is the connection's API host |

Also: the provider name and the connection travel together (`CONNECTION_TARGET_MISMATCH` otherwise);
the idempotency hash includes the connection, so one key cannot name two connections; billing
evidence records `billing_owner = USER_CONNECTION`; `JOB_CREATED` carries the same. Inline provider
results may now be video (`video/mp4`, `video/quicktime`, `video/webm`), validated like a download.

`USER_CONNECTIONS_ENABLED=false` stops every new connection use (chat and generation) without
touching platform jobs.

## 3. Canvas workflows

### 3.1 The document

`core/workflows/workflow_core/`. `GET /v1/workflows/node-types` serves the catalogue the editor
renders from and the engine validates against.

| Node | Inputs → outputs | Runs as |
| --- | --- | --- |
| Text | → text | resolved when the run starts |
| Image | → image | the uploaded project asset, resolved when the run starts |
| LLM | prompt (text), context (text, up to 8) → text | chat on a connection |
| Image generation | prompt (text), references (image, up to 6) → image | `source=platform`: admission + credits (quality tier); `source=connection`: the gateway's connection path |
| Video generation | prompt (text), first frame, last frame, references (image, up to 4) → video | as above; platform model or Automatic, duration, aspect, resolution, negative prompt, audio (connection) |
| Note | - | never runs |

Graph document `canvas-graph-v1`: nodes `{id, type, position, data, label?}`, edges
`{id, source, source_port, target, target_port}`, `viewport`. **A save** (`parse_graph`) requires a
well-formed document: known types and ports, same-type edges, input capacity, no cycle, no self
loop or duplicate, and valid values for every parameter that is set. A half-built canvas saves.
**A run** (`run_issues`) additionally requires, within the nodes it will execute, every visible
required parameter and each node's own rules (an LLM needs an instruction or a connected prompt, a
generation needs a prompt, a connection source needs a connection and a model, a last frame needs a
first frame), reported all at once, keyed by node.

Saves are optimistic: `PUT /v1/workflows/{id}` names `base_version`; a stale save answers `409
WORKFLOW_VERSION_CONFLICT` with the current version instead of overwriting.

### 3.2 A run

`POST /v1/workflows/{id}/runs {base_version?, node_ids?, force?}`:

- One transaction snapshots the graph into `workflow_runs`, creates a `workflow_node_runs` row for
  every executable node in scope (all, or the named nodes plus everything that feeds them),
  resolves Text/Image nodes, and satisfies from an earlier result any node whose **fingerprint**
  matches (§3.3) unless forced. A second active run of the same canvas is refused
  (`WORKFLOW_RUN_ACTIVE`).
- The worker advances runs on its own asyncio task (`workflow_run_loop`,
  `WORKFLOW_RUN_INTERVAL_SECONDS`), beside - not inside - the loop that moves paid jobs, so a
  minutes-long model call never holds generation jobs up. `POST /internal/maintenance/workflow-runs`
  runs the same advance.
- A run is claimed under a lease that outlives the longest model call
  (`max(WORKFLOW_RUN_LEASE_SECONDS, WORKFLOW_LLM_TIMEOUT_SECONDS + 60)`). A node is ready when every
  node feeding it succeeded; a node with a failed, skipped or cancelled input is `SKIPPED`.
- **LLM nodes** call the connection's chat API; a call that outlived a dead worker is retried once.
- **Generation nodes** create one generation job with idempotency key `canvas:<node run id>`; a
  crash between creating the job and recording it replays to the same job, and a node that lost
  the id recovers it from that key. The node then follows its job: `COMPLETED` with an asset
  succeeds, `FAILED`/`CANCELLED`/`WORKER_NEEDS_USER_ACTION` fails with the job's own reason.
  Platform nodes go through `GenerationAdmissionService.admit_passenger` (the development bypass
  keeps its latitude, a real user's plan applies) and `VisualProductionRuntime.submit_passenger`
  labelled `mode = CANVAS` with the workflow, run and node ids; they are priced, reserved and
  settled exactly like the Create page. Out of credits fails the node as `INSUFFICIENT_CREDITS`.
- Every node transition is a conditional update on the node's current status (and attempt), so a
  stop that lands during a model call wins and the late answer is dropped.
- A run view carries `scope`: `ALL` when every executable node of the snapshot was asked for,
  `NODES` for a run of selected nodes (plus what feeds them).
- `POST /v1/workflow-runs/{id}/cancel` stops the run and its pending/running nodes, then asks the
  gateway to cancel the jobs they created. Deleting a canvas stops its active run.

### 3.3 Reuse

A node's fingerprint is the SHA-256 of its type, its **visible** parameters and, in port order, the
fingerprints of whatever feeds it. Layout, notes and parameters the node does not use change
nothing; an edit changes that node and everything downstream. "Run all" reuses every unchanged
node's latest successful result (`CACHED`, no new charge); "Run this node" and "Re-run all" force.
A cached generation result is reused only while its job is `COMPLETED` and not deleted from
Productions (whose media may have been reclaimed).

## 4. The web app

`apps/web/canvas/` - vanilla ES modules like the rest of `apps/web`, no framework or library:

| Module | Role |
| --- | --- |
| `graph.js` | The pure document model: typed port rules (the server's, mirrored), capacity, cycle prevention, copy/paste, snapshot undo/redo with batching, run validation (`graph.test.mjs`, `node --test`) |
| `editor.js`, `node-card.js` | The infinite canvas: one transformed node layer plus one SVG edge layer; pan, cursor-anchored zoom, marquee, drag-to-connect with compatible-port highlighting, quick-add on an empty drop, minimap, keyboard shortcuts, file drop |
| `inspector.js`, `fields.js` | Node settings rendered from the catalogue (`visible_when`), connection/model pickers, platform model picker, uploads, result and billing details |
| `connections.js` | The API connections dialog: protocol cards, presets, suggested and remote models, save-then-check |
| `canvas-app.js`, `palette.js`, `ui.js`, `media.js`, `api.js`, `icons.js` | The shell: projects, canvases, autosave with `base_version` (409 banner), runs and polling, credits and wallet, authenticated media as object URLs |

`/app` and `/app/canvas/<id>` mount `#canvasShell`; `/app/studio` mounts the previous workbench, which
gained a "Canvas" link and names a connection job "Your API · <model>" instead of a BestShiny tier.
`app.js` stays the only auth owner: the canvas signs out through `ai-director:request-logout` and hands a
`401` to `ai-director:session-expired`, which runs the same `lockAuth()`. The wallet is reused through
`#walletBtn` and `ai-director:workspace-changed`. A new project opens "My first canvas" seeded with
Text → Video generation and a note pointing at API connections.

Names, texts, model ids, model output and provider error messages are all untrusted: every one reaches
the DOM through `escapeHTML` (`ui.js`) or `textContent`, never raw into a template. This was checked in
a browser on 2026-09-14 with an HTML payload in every user-controlled field (project, canvas and node
names, text and note nodes, connection name, model id, LLM output), across the canvas, inspector,
menus, viewer, connections dialog and the studio's lists; nothing executed. Keep new templates to the
same rule.

## 5. Settings

| Variable | Default | Notes |
| --- | --- | --- |
| `USER_CONNECTIONS_ENABLED` | `true` | kill switch for all connection use |
| `USER_CONNECTION_ALLOW_PRIVATE_NETWORK` | `false` | development only; refused in production |
| `USER_CONNECTION_HTTP_TIMEOUT_SECONDS` | `120` | per provider request |
| `USER_CONNECTION_MAX_CONCURRENT_JOBS` | `4` | per connection |
| `USER_CONNECTION_MAX_RESPONSE_BYTES` | `67108864` | per JSON response |
| `USER_CONNECTION_MOCK_PROTOCOL_ENABLED` | `false` | development/test only; refused in production |
| `WORKFLOW_RUN_INTERVAL_SECONDS` | `2` | `0` disables the worker loop |
| `WORKFLOW_RUN_BATCH_LIMIT` | `20` | runs per advance |
| `WORKFLOW_RUN_LEASE_SECONDS` | `300` | raised automatically above the LLM timeout |
| `WORKFLOW_LLM_TIMEOUT_SECONDS` | `300` | one model call |

`CREDENTIAL_ENCRYPTION_KEY` must be set and identical in the api and the worker: the api encrypts
a key and the worker decrypts it. With an empty key each development process invents its own, and
the worker then reports `CONNECTION_KEY_UNREADABLE`.

## 6. Deploying

One migration (`0083`): four new tables and one nullable `generation_jobs.connection_id` column with
an index. Follow the DDL order in `docs/DEPLOYMENT.md` (stop worker → `up -d api web` → head →
`up -d worker`). No `.env` change is required; the defaults keep connections on and the development
switches off. The web bundle changes (`--force-recreate web`).

## 7. Tests

`tests/test_user_connections.py` (ownership, secrecy, wire formats, egress),
`tests/test_gateway_user_connections.py` (who pays, whose key, the boundary, the wire through a
connection, the media fence), `tests/test_workflow_graph.py` (document rules, fingerprints),
`tests/test_workflow_runs.py` (API, runs, reuse, failure, cancellation, platform nodes), and
`apps/web/canvas/graph.test.mjs` (`node --test`).

## 8. Open items

- **No live call has been made on any connection protocol** from this branch. The Anthropic request
  follows Anthropic's Messages API documentation; the OpenAI-compatible request is the standard Chat
  Completions shape and was not re-checked against each preset vendor's current reference; the
  video/image paths reuse the platform's reviewed adapters. A real key has not exercised any of them
  end to end. On this laptop the fake-IP proxy
  makes every provider host resolve to `198.18.x.x`, which the fence correctly refuses; verify on
  the server.
- LLM nodes run only on connections. There is no per-user metering for platform chat models, so
  exposing them on the canvas would be unmetered platform spend; a platform LLM node needs a
  pricing and credit decision first.
- No free-form "custom HTTP" video protocol: every video protocol is a reviewed adapter. A user
  whose provider is not OpenRouter, Ark or DashScope cannot connect it yet.
- Ark and DashScope have no key-check endpoint the platform has reviewed; their test is local and
  says so, and a bad key surfaces at first use.
- The canvas does not yet expose the studio's director, screenplay, continuity or character tools
  as nodes; those remain in `/app/studio`.
- Web: only the graph model has automated tests (`graph.test.mjs`); the editor, inspector and dialog
  were verified by hand in a browser against the real API and worker. Touch pinch, space-drag and
  middle-button pan are implemented but were not exercised. Edits made after the last autosave are
  lost when a session expires. The canvas keeps its own copy of the studio's public model-name
  table (`MODEL_LABELS`), which must be kept in step. A video result downloads whole on first play,
  as the studio's does (authenticated storage cannot be range-streamed through an object URL).
