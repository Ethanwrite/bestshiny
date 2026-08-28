# Open Issues — everything left unresolved

Snapshot: 2026-08-27 · live gate OPEN · Wan T2V/I2V/R2V verified · router evidence landed, LCB off · model contract aligned, 24 models · companion to [`../HANDOFF.md`](../HANDOFF.md)

Every item this session raised and did **not** finish, in one place. Section 1 needs you.
Sections 2–4 are engineering work and need no decision from you.

Items closed on 2026-08-23 are listed in section 5 rather than deleted, so a reader of the
previous snapshot can see where each one went.

---

## 1. ACTION REQUIRED FROM YOU

Nothing below can be done by the agent — each is either a cost decision, a credential, or a
product judgement.

### 1.0 Before you flip anything: run the pre-flight

```bash
uv run python scripts/preflight_live.py
```

No network call, no secret printed. It answers the question that matters before spending:
per generation path, would this reach the provider, fail closed here, or **fail at the
provider after being billed**. As of this snapshot it reports Wan T2V as the only live-
testable path — see §1.3 for why I2V and R2V are not.

### 1.1 Live calls are ON — every provider transport is billable

All three gates are set, on your instruction (2026-08-25):

| Variable | Now |
| --- | --- |
| `PROVIDER_MODE` | `live` ✅ |
| `ALLOW_LIVE_PROVIDER_CALLS` | `true` ✅ |
| `LIVE_PROVIDER_CONFIRMATION` | `I_UNDERSTAND_THIS_COSTS_MONEY` ✅ |

`PLATFORM_API_KEY` and `CREDENTIAL_ENCRYPTION_KEY` were generated locally and written to
`.env`; both are self-generated entropy, not third-party credentials. A live *generation*
through the Gateway additionally needs a `LiveCanaryPermit` via
`POST /internal/live-canary-permits`.

20 of 22 models are `live_enabled` after `POST /internal/models/reconcile-live?apply=true`.
The two that stay shut are `grok-video-official` and `veo-3.1-quality-official`, whose
providers are reserved stubs with no transport.

For RunAPI edge calls specifically, also set `ALLOW_RUNAPI_EDGE_CALLS=true` (currently
`false`). `RUNAPI_BASE_URL` and `RUNAPI_MODEL_ID` are configured.

### 1.2 Verifying the image model live — one command, and what it costs

`openai/gpt-image-2` is implemented and covered by 30 offline tests, but no request has ever
reached OpenRouter. Two opt-in tests exist in `tests/live/test_openrouter_image_live.py`:

```bash
uv run pytest --run-live-provider -m live_provider tests/live/test_openrouter_image_live.py
```

- `test_capability_descriptor_matches_the_reviewed_envelope` is a **free** `GET`. It re-reads
  the model's published limits and fails if the envelope compiled into the adapter has drifted.
  Run this one freely.
- `test_smallest_approved_generation_returns_decodable_image_bytes` **is billed** — one
  1024×1024 `quality=low` image, about **USD 0.01** at the recorded rate
  (`output_image` tokens at USD 0.00003 each). A `quality=high` image is about USD 0.12.

Both are skipped without `--run-live-provider`, and both still fail closed on the §1.1 gate.
That was verified: with the gate incomplete the run stops at
`live provider call denied; missing: LIVE_PROVIDER_CONFIRMATION=...` before any socket opens.

### 1.3 Object storage is required before any reference media works

**This is now the binding constraint on testing the Wan work.** Of the three Wan 2.7 modes,
only T2V carries no media. I2V and R2V each need a URL Alibaba fetches itself, and there are
only two ways to produce one:

| Backend | Result in live mode |
| --- | --- |
| S3-compatible (`S3_*` set) | a real presigned URL — the intended shape |
| Local disk + `LOCAL_REFERENCE_SIGNING_KEY` | `http://localhost:8080/...`, which live mode **refuses** for not being HTTPS — and which Alibaba could not reach even if it were |

So an I2V or R2V shot fails closed before submission today. That is the correct behaviour, but
it means the reference plane, the `media[]` payload and the R2V matrix cannot be verified live
until a bucket exists. Configure S3/R2/MinIO and an HTTPS `PUBLIC_BASE_URL`, or accept that the
live evidence covers T2V only.

Every reference-carrying shot — and every image *edit* — fails closed with
`PROVIDER_REFERENCE_URL_UNAVAILABLE` until a bucket exists. `LOCAL_REFERENCE_SIGNING_KEY` is
set for local development and **does** proxy bytes through this service; leave it empty in
production and set `PUBLIC_BASE_URL` to an HTTPS origin.

Why this matters beyond correctness: with the API in the media path, a dozen concurrent 4K
reference edits make the control plane an image CDN — the tier least able to absorb it.

### 1.4 Google Flow video will fail closed until you supply a model key

`FLOW_VIDEO_MODEL_KEYS` is empty and only the legacy `veo` alias ships with a reviewed key.
`flow-veo-3.1` therefore returns `FLOW_MODEL_KEY_NOT_MAPPED`. This is deliberate — the
previous behaviour silently rendered on `abra_t2v_{duration}s`, a different model. Supply the
reviewed runtime key, e.g. `FLOW_VIDEO_MODEL_KEYS=flow-veo-3.1=abra_veo31_{duration}s`.

### 1.5 Omni Flash needs a transport decision

Omni Flash is **Google Gemini Omni Flash**, not a ByteDance model. `providers/omni` is an
unconfigured stub. Tell the agent whether it arrives via a Gemini API key or through your own
Google Flow reverse proxy, and it can be registered. This is also what blocks the Omni entry in
`skills/model-prompting/references/`.

### 1.14 The Grok Build balance is exhausted, and six research pairs are unfinished

The 2026-08-26 router-evidence research ran the Grok CLI over eleven models × three layers. A
second pass, to pick up a fix to the research schema, ran the account out of balance partway
through:

```
API error (status 402 Payment Required): Grok Build usage balance exhausted
```

What survived: 16 of the 33 model×layer files are the first pass (repaired at ingest), 11 are the
corrected pass, and **6 pairs have no usable research at all** — they are recorded as gaps in the
layer files rather than left silently empty:

```
benchmark_prior   kling-3-standard-openrouter
community_prior   gpt-image-2-openrouter, kling-3-pro-openrouter,
                  veo-3.1-quality-official, wan-2.7-official
official_prior    veo-3.1-quality-official
```

Nothing is broken by this — the layers are complete and consistent, the gaps are recorded, and the
coverage report names them. Closing them needs a balance and one command:

```bash
.venv/bin/python scripts/research_router_evidence.py --overwrite
```

Only you can top the balance up. Cost of the first two passes was roughly USD 10 of Grok credit.

### 1.16 One-time: retire the empty candidates the old batch flow left behind

Added 2026-08-28 with the batch-atomicity change. The retired pre-creation scheme could strand
empty `CREATED` candidate rows (batch siblings allocated before their media existed, orphaned by a
crash). The current pipeline can no longer produce them; existing databases may still hold some.
Against the production database, once, after the change is deployed:

```bash
.venv/bin/python scripts/retire_empty_candidates.py                # audit — read-only JSON
.venv/bin/python scripts/retire_empty_candidates.py --apply        # fenced retirement
```

The audit only matches rows that are `CREATED`, bound to no job from either direction, own no
media, and are older than 24 hours; retirement is a status change to `RETIRED` plus a
`DecisionRecord` per row, never a delete. Cost: none. Risk: none beyond reading the report first,
which is what the two-step shape is for.

### 1.15 The conservative LCB cannot be enabled yet, and that is a data question

`FEATURE_ROUTER_LCB` is `false` and must stay false until a replay passes. A replay needs
production observations, and `router_observations` is empty because the recorder only started
existing on 2026-08-26. It fills on its own as the platform is used — one row per evaluated
generation — but nothing an agent can do makes that happen faster.

```bash
.venv/bin/python scripts/router_posterior_run.py   # exit 2 today: fewer than 20 observations
```

When it exits 0, the flag becomes a decision you can make. When it exits 3, the replay ran and the
posterior policy was **not** better than what is already running, and the flag should stay off —
`failure_reasons()` in the report says which of regret, failure rate, cost or interval calibration
failed. See [`ROUTER_EVIDENCE.md`](ROUTER_EVIDENCE.md) §7–8.

### 1.6 Rotate the keys pasted into chat

Ark, DashScope, RunAPI and a GitHub PAT were pasted into the conversation transcript. Your earlier
"no rotation needed" decision covered keys that only ever lived in your local environment;
these did not.

### 1.7 Product decision: deterministic fallback

Before model-backed prompt compilation is built: when the model returns invalid JSON or fails
fact-lock validation, may the system fall back to the current deterministic compiler, or must
it fail closed? This must be decided explicitly and recorded in the audit trail. It is not an
implementation detail.

### 1.8 Product decision: Chinese → English scope

Requested but **not done**, because a blind conversion would delete product capability. Of 34
files containing Chinese, almost none is comments:

| Category | Examples | Effect of translating |
| --- | --- | --- |
| Functional NLP logic | `edge.py` numeral/negation regexes, `corrector.py` (82 keyword lines), `narrative_core/compiler.py`, camera-gaze patterns in `video_prompt_core/compiler.py` | **Deletes Chinese-language support** from a `zh-CN` product |
| Product UI | `apps/web/index.html` (121), `apps/web/app.js` (165), `wallet.js` | Changes the product's language — a product decision |
| API messages | `auth.py` (20), `main.py` (6), `payment_routes.py` (8) | User-facing text changes |
| Docs and display strings | docs, one `openai.yaml` display name | Safe |

Tell the agent which categories to convert.

### 1.9 Wan 3.0 needs your Beta invitation before it can be promoted

`VIDEO_WAN` primary is Wan 2.7 per your kernel spec, with 3.0 as an explicit fallback. Wan 3.0
has a 30-second native single-shot envelope, which is the direct fix for the compressed-shot
character fragmentation you described. The envelope is recorded in
`skills/model-prompting/references/wan.md` either way.

**Partly resolved 2026-08-27.** A second route to the same model now exists and needs no
invitation: `wan-3.0-openrouter` (`alibaba/wan-3.0`), verified against OpenRouter's own
`GET /api/v1/videos/models`, is registered, enabled and declares the 30-second envelope. Long-form
shots therefore have a route today — which also closes §2.26 below.

The DashScope route still needs your invitation. Its ID has since been read from DashScope's own
documentation as `wan3.0-video` (the earlier note that it was never verified was true when written),
but `wan-3.0-official` stays **disabled**, because this account has no Wan 3.0 access there. The two
are deliberately separate records: same model, different transports, different entitlements. When
your invitation arrives:

```
WAN_VIDEO_MODEL_KEYS=wan-3.0=<the model ID DashScope issues you>
```

The model definition and its FALLBACK binding stay registered meanwhile — the router and the
capability resolver read them as capability records, exactly as `VIDEO_GROK` and `VIDEO_VEO` do.

### 1.13 Two open items on the model registry

**Veo envelope confirmed.** `google/veo-3.1`, `google/veo-3.1-fast` and `google/veo-3.1-lite` were added on
2026-08-25 from operator-supplied ids: 4/6/8s, 720p/1080p, text and image input, first/last frame, and
synced audio on all three variants. Audio was declared on the main variant first and extended to Fast and
Lite once the operator confirmed it — under-declaration costs a capability, over-declaration costs a billed
request that fails at the provider, so the order was deliberate.

**They are `enabled` but not `live_enabled`.** A newly seeded model does not open itself; adding a
credential never does either (§2.5). One call opens all three, since `OPENROUTER_API_KEY` is present:

```bash
curl -XPOST -H "Authorization: Bearer $PLATFORM_API_KEY" \
  "localhost:8080/internal/models/reconcile-live?apply=true"
```

Run it without `?apply=true` first to see what it would change.

**No live call has been made through the OpenRouter video route for Veo.** Kling and Grok Imagine already
use `POST /videos` on OpenRouter, so the transport is exercised; these three model ids are not. The first
call is also the first verification.

### 1.10 Layer 2 is on, and locking now refuses rather than degrades

`FEATURE_SEMANTIC_STYLE_LOCK` is `true` and `STYLE_SEMANTIC_EMBEDDING` resolves to
`google/gemini-embedding-2`. As of 2026-08-25 this is **enforced, not advised**: if the embedding call cannot
produce a reference, the lock is refused — `503` when the model was unreachable, `409` when the reference
media could not be read — and nothing is written. The project stays lockable.

That replaces the standing warning that used to live here. Previously a lock made while layer 2 could not run
committed anyway with one layer, and since `ProjectStyleLock` is append-only and a database trigger forbids
re-locking, **that single gate was permanent**. The advice was to lock only after going live; the code now
enforces it.

`scripts/preflight_live.py` reports the state of this path before you try:

```bash
.venv/bin/python scripts/preflight_live.py
```

It currently says `AT RISK` — the gate is open and `OPENROUTER_API_KEY` is set, but
`google/gemini-embedding-2` has still never been called (§3.2, §3.5), so the first lock attempt is also the
first live exercise of that model. A refused lock costs nothing and can be retried; locking is the one action
in this system that cannot be undone.

Two consequences of the layer itself:

- **Cost.** One `google/gemini-embedding-2` call per locked style, and one per evaluated candidate. Roughly
  USD 0.00045 per image, so a 60-episode series at a few hundred candidates is cents, not dollars — but it is
  a per-candidate paid call on the commit path.
- **Meaning.** It changes what "committable" is. A candidate that layer 1 passes can now fail on rendering
  medium, and a candidate whose semantic evidence is unavailable becomes `REVIEW_REQUIRED` rather than
  passing. That is the intended behaviour — a missing second opinion is not a passing one — but it means an
  outage at the embedding provider turns into a review queue rather than silent approval.

`semantic_similarity_threshold` defaults to 0.80 and is uncalibrated — the model has never been called, so the
real distribution of its similarity scores is unknown. Expect to tune it against the first live batch rather
than trusting the default.

---


### 1.11 Decided — PostgreSQL everywhere, alembic owns the schema

Answered 2026-08-25: both halves. `build_container()` refuses a non-PostgreSQL `DATABASE_URL` under
`DEPLOYMENT_ENVIRONMENT=production`, startup no longer creates tables, and local `.env` points at the
compose PostgreSQL, which is published on `127.0.0.1:5432` for host-side alembic and pytest.

What you need to know to run it:

```bash
docker compose up -d postgres
```

Then `alembic upgrade head` before starting the application — startup will refuse a database that is not
at `REQUIRED_SCHEMA_REVISION`, and will name that command when it does. `DEPLOYMENT_ENVIRONMENT` is still
`production` in `.env`; that is now satisfiable, but it does mean this machine enforces production rules
(`AUTH_REQUIRED`, `PLATFORM_API_KEY` entropy, PostgreSQL). Set it to `development` if you would rather it
did not.

The test matrix is a flag:

```bash
.venv/bin/python -m pytest -q                        # SQLite — fast, algorithm-level
POSTGRES_PASSWORD=... .venv/bin/python -m pytest -q --database=postgres
```

The PostgreSQL half reroutes the shared `container` fixture into a throwaway schema in a dedicated
`video_platform_test` database. Test modules that build their own `Settings` with a hard-coded SQLite URL
(about fifteen of them) are **not** rerouted and still run on SQLite in both halves.

### 1.12 Decided — subscription grant against one ledger (half built)

Answered 2026-08-25: every tier reserves and settles against the same wallet, and the plan sets a periodic
credit grant plus discounts and model entitlements. A plan no longer decides *whether* a generation is
charged.

**Built** (§2.17): the charge itself. FREE, PRO and ENTERPRISE all reserve, settle, refund and hold for
reconciliation through one service. Running out answers 402 rather than 403.

**Already existed, and I was wrong to say otherwise:** a top-up path. `POST` through the DePay checkout
credits *any* workspace in {FREE, PRO} and appends a `WorkspaceCreditLedgerEntry`; `GET
/api/workspaces/{id}/wallet` shows balance, reserved and purchased credits. A paid workspace that runs out is
not stuck — it tops up through the same link that upgrades a FREE one.

**Not built:** the *recurring* half. There is no billing period on `Workspace`, no renewal, and no grant
primitive — the only ways credits appear are the 50 at workspace creation and a DePay purchase. A
subscription grant needs a period, an idempotent grant operation keyed to that period, and something that
fires on renewal. None of the three exists, and building the operation without the period would be a lever
attached to nothing.

**Also not built:** the plan *discount*. `CreditPricingEngine` has a `service_multiplier`, and no plan tier
feeds it. Every tier pays the same credits for the same generation today.

See also §2.24: the 50-credit grant buys exactly one 4-second clip and nothing longer, and a paid tier that
has not purchased has the same 50 — which is the more urgent half of this decision.


---

## 2. Known defects — not fixed

| # | Defect | Where |
| --- | --- | --- |
| 2.1 | **Fixed 2026-08-27 — which earlier beat matters is no longer a similarity decision.** Explicit dependencies are rows (`shot_dependencies`, migration `0052`): FORESHADOWING, FACT_REVELATION, OBLIGATION_FULFILLMENT and STATE_INHERITANCE, each held to the referent that makes it checkable, written by script compilation (STATE_INHERITANCE for continuous pairs) or by manual editing (`POST/DELETE /v1/shots/{id}/dependencies`). Retrieval is now two-stage: stage one force-resolves every declared dependency and the ledger's open obligations into context (provenance `EXPLICIT_DEPENDENCY` / `OPEN_OBLIGATION`), stage two supplements by similarity (`SIMILARITY`) — the budget sheds similarity first and raises rather than dropping a forced segment. A dependency that cannot be resolved moves the shot to `USER_REVIEW_REQUIRED` with a `SHOT_DEPENDENCY_RESOLUTION` decision record and refuses generation; it never degrades to similarity-only context. Residual: retrieval stage two is still keyed on the current shot's prompt text — un-declared callbacks remain a similarity bet, which is what manual dependency editing is for. | `core/narrative-ledger/narrative_ledger_core/dependencies.py`, `services/production-engine/production_engine/runtime.py` |
| 2.2 | Aggregate style drift across episodes is unmonitored — each shot can pass while the series slowly walks away from episode 1. | `core/style/style_core/service.py` |
| 2.3 | `timeline_scope_key` branch proliferation (flashback/dream) has no merge or retirement policy. | `core/character/` |
| 2.4 | **Fixed 2026-08-25 — a synchronous provider's result is now durable.** It was held in a Gateway attribute between the confirmed submission and the poll that consumes it, both inside one `process()` call; process death in that window lost an artefact the workspace had already been billed for. Migration `0040_provider_synchronous_result_inbox` adds `provider_synchronous_results` and its ordered `provider_synchronous_result_outputs`. The result is written in the **same transaction** that confirms the submission, so it becomes durable exactly when the workspace becomes liable, and it is deleted in the **same transaction** that marks the job terminal — reading never consumes it, because delete-on-read would only move the fatal window to "between the read and the completion commit". Each output carries a SHA-256 that is re-checked on read; a mismatch fails `submitted=True`, so a corrupt artefact reaches `RECONCILIATION_REQUIRED` rather than being published as a paid result. A stale row from an earlier attempt names a different `provider_job_id` and is discarded, never returned. A test drives the completion from a **different** `GenerationGateway` object, sharing nothing with the first but the database. | `services/generation-gateway/generation_gateway/gateway.py` |
| 2.6 | `MediaRenditionKind.THUMBNAIL` exists in the schema and nothing generates one. The Web UI still reads originals, so a gallery of 4K plates downloads 4K plates. | `services/media-service/media_service/renditions.py` |
| 2.7 | Derived renditions are never retired. They are content-addressed and bounded, but a provider that repeatedly changes its limits accumulates one copy per constraint set with no garbage collection. | `services/media-service/media_service/renditions.py` |
| 2.8 | The batch-to-candidate path allocates sibling candidates in a short transaction before the media is registered. If the process dies between the two, the shot keeps empty `CREATED` candidates. They are inert — no output asset, so nothing can commit them — but nothing sweeps them either. | `services/generation-gateway/generation_gateway/gateway.py` |
| 2.9 | **Fixed 2026-08-27 — video is adapted to declared bounds, and only to declared bounds.** `ProviderReferenceConstraints.video` (`VideoReferenceConstraints`) declares container, codec, frame size, bitrate, frame rate, duration, byte and aspect-ratio limits. A declared consumer always validates: the original is ffprobed even when it fits, a failing one is transcoded by ffmpeg off the event loop (remux-only when the container is the only gap), and every derived copy is **re-probed against the full constraint set before it is stored** — a provider is only ever handed a validated encoding. Renditions are cached by source sha256 + full constraint key + transcoder version. Semantic edits stay refused: over-long duration and mismatched aspect ratio fail closed naming the violated constraint (`VIDEO_DURATION_EXCEEDS_LIMIT`, `VIDEO_ASPECT_RATIO_NOT_ACCEPTED`), because trimming and cropping change content and belong to an explicit human act, and an unmeetable byte/bitrate budget names itself the same way. **Residual:** no shipped provider declares `video` yet — declarations must be read from vendor documentation per provider (Wan R2V `reference_video` first), not guessed, so until then video references keep failing closed exactly as before. | `services/media-service/media_service/video_renditions.py`, `packages/provider-sdk/provider_sdk/base.py` |
| 2.10 | A direct upload is validated from a bounded 64 KB header, not a full decode. Magic bytes, declared format and pixel dimensions are checked; a **truncated or internally corrupt** file passes and fails later at first use, where `RenditionResolver` raises `RenditionDerivationFailed`. Deliberate — pulling every upload back through the API to catch it one step earlier would undo the reason writes bypass the API — but it is a real difference from the multipart path. | `packages/shared/platform_shared/media_validation.py` |
| 2.11 | **Fixed 2026-08-25 — the sweep is atomic and it runs.** Two problems behind one endpoint. It **raced**: expired rows were read with no lock, the hold was released in one transaction and the row abandoned in another — the opposite order from completion, which locks the `DirectUpload` row first and its reservation second. A sweeper could therefore release a hold out from under a completion that already owned the row, whose `settle_in` then raised `StorageReservationConflict` into a handler that did not exist: **500** for an upload the client had finished correctly. `DirectUploadService.claim_expired` now takes the row lock first, re-reads the `PENDING`/expiry predicate under it, and the caller releases through `WorkspaceStorageQuota.release_in` in the same transaction — one commit or none, so a conflicting release rolls the abandon back and the row stays sweepable. Completion answers **409** on a settlement conflict from any other cause. A `postgres_only` test forces the interleaving and fails if the `FOR UPDATE` is removed. And **nothing scheduled it**: the endpoint existed and no cron, worker or scheduler called it, so "the sweep is done" only meant "the manual endpoint is done". `media_service.maintenance.sweep_expired_uploads` is now one implementation called by both the endpoint and the worker loop, on `EXPIRED_UPLOAD_SWEEP_INTERVAL_SECONDS` (default 300, `0` disables) and `EXPIRED_UPLOAD_SWEEP_LIMIT` (default 200) — due immediately on worker start, never fatal to the job loop. Unchanged on purpose: the object is never deleted, so an abandoned upload still leaves one orphan in the bucket, and stale `RESERVED` holds with no `PENDING` upload behind them are reported for reconciliation and deliberately not released. | `services/media-service/media_service/maintenance.py` |
| 2.12 | **Savepoints do not roll back under pysqlite.** Work inside `session.begin_nested()` survives a rollback of the enclosing transaction — verified directly. Seven call sites rely on savepoints (`registry.py` ×2, `renditions.py`, `gateway.py`, `affinity.py` ×3). **No longer reachable in production** (2026-08-25): `build_container()` refuses a non-PostgreSQL `DATABASE_URL` when `DEPLOYMENT_ENVIRONMENT=production`, and local `.env` now points at the compose PostgreSQL. It remains true of any SQLite database, which is why the test matrix runs the shared `container` fixture on PostgreSQL too (`pytest --database=postgres`). The documented pysqlite workaround (`isolation_level = None` plus an explicit `BEGIN`) was tried and reverted: it makes every transaction take a write lock, and the concurrency suites either fail on `database is locked` or hang. | `packages/database/platform_database/session.py` |
| 2.13 | **Half-confirmed 2026-08-25.** The R2V `ratio` condition is an inference, not a quoted rule. The documentation gives "R2V → resolution + conditional ratio" without naming the condition; this adapter sends `ratio` only when no first frame is supplied, on the same logic that makes I2V take no ratio at all — a supplied frame already fixes the aspect, and sending both asks one question twice. The live R2V canary (task `57ba09a0`) confirms the half this platform actually sends: R2V **with no first frame** accepts `parameters.ratio` and completes. What stays unconfirmed is the other half — whether R2V *with* a first frame would reject a ratio or merely ignore it — which would cost another clip to establish and is only reachable by deliberately sending a combination this adapter refuses to build. Everything else in the Wan request body was re-derived from the published API references (HANDOFF §12d). | `providers/wan/wan_provider/adapter.py` |
| 2.14 | `supports_reference_voice` is a first-class capability on **every** model profile and is `false` everywhere **except Wan 2.7**, which was corrected to `true` on 2026-08-25: R2V nests a `reference_voice` audio URL inside a reference material and I2V takes a `driving_audio` media entry, and the flag means an asset the model conditions *on* (as against `supports_audio`, audio it produces). Every other model inherits the column default and has not been reviewed. A model that does accept one is under-declared rather than mis-declared — it fails closed — but the flag should be reviewed per provider rather than assumed. Wan 2.7's own reading was wrong in exactly this way for a week. | `config/model-registry/defaults.json` |
| 2.15 | **Resolved 2026-08-25 — the database now has one schema authority.** `Database.create_all()` no longer runs at startup. `build_container()` checks the stamped revision against `REQUIRED_SCHEMA_REVISION` and refuses to start otherwise, naming `alembic upgrade head`; alembic alone creates and alters schemas. `create_all_and_stamp()` survives for throwaway databases (a per-test tmp file, a scratch simulation) and runs only under `DEPLOYMENT_ENVIRONMENT=test`. A test asserts the constant equals the alembic head, so bumping one without the other is a gate failure rather than a runtime surprise. | `packages/database/platform_database/session.py` |
| 2.16 | Importing `video_platform_api` builds a container as a side effect: `__init__.py` imports `main`, and `main.py` ends with `app = create_app()` at module scope. Any import — a script, a test collection, a linter plugin — therefore opens `DATABASE_URL`, runs `create_all()` and seeds defaults. It is why the live Wan test, which touches no database, could not run until it was pointed at a scratch one. | `apps/api/video_platform_api/main.py` |
| 2.5 | Startup still never replays defaults over an administrator's changes, which is right. `POST /internal/models/reconcile-live` is now the operator path that was missing: it re-derives `live_enabled` for every model from the credentials present, reports by default and writes on `?apply=true`. It moves only enablement — never the execution ID — so it cannot overwrite an administrator's chosen model. **Nothing calls it automatically**, so adding a credential still requires that call. | `apps/api/video_platform_api/main.py` |
| 2.17 | **Fixed 2026-08-25 — every plan draws on the same wallet.** `reserve_generation` returned an inert charge for any tier other than FREE, so PRO and ENTERPRISE generations were quoted, the quote was written onto the job, and then nothing was reserved, nothing settled, and an ambiguous provider result left no credit to hold for reconciliation. Who pays is now one property — `WorkspaceCreditBalance.billable` — used by both the service and the Gateway: every plan does; a project with no workspace and the `ALL` workspace do not. `ALL` is the authentication-disabled local development bypass, not a tier — it still receives server pricing and CostRecords, and charging it would make local development spend a real balance. Running out of credits is now reachable for a paid tier, so it answers **402** where a plan entitlement denial answers **403**: two problems with two different fixes, top up versus upgrade. | `core/entitlements/entitlement_core/credits.py` |
| 2.18 | **Fixed 2026-08-25 — the style lock fails closed on its second layer.** With `FEATURE_SEMANTIC_STYLE_LOCK=true`, an unavailable `google/gemini-embedding-2` used to record `style_layers: 1` and a `semantic_layer_absent_reason` and commit — and because `ProjectStyleLock` is append-only with a trigger forbidding re-locking, a transient outage permanently downgraded that project to a single-layer gate that looked identical to one made deliberately with the feature off. `lock()` now raises `SemanticStyleLayerRequired` and writes nothing, so the project stays lockable and a retry produces the two-layer lock that was asked for. The route answers **503** when the model was unreachable (waiting can help) and **409** when the reference media could not be read (it cannot). With the feature off, a single-layer lock is still the intended outcome and still records `SEMANTIC_EMBEDDER_NOT_CONFIGURED`. | `core/style/style_core/service.py` |
| 2.19 | **Fixed 2026-08-25 — a vector now carries the space it belongs to, and spaces are compared before vectors are.** `StyleEmbedding` gained `model_revision`, `normalization` and `distance_metric` (migration `0041_embedding_space`), completing the space alongside the `provider`, `model`, `algorithm_version` and `dimension` it already had — of which only `model` was ever read, and only to find a row. `EmbeddingSpaceIdentity` is compared at three points: before reusing a stored reference to lock (refuses the lock, non-retryable), and before scoring either layer of a candidate (`REVIEW_REQUIRED` with `STYLE_EMBEDDING_SPACE_CHANGED:<fields>` / `STYLE_SEMANTIC_EMBEDDING_SPACE_CHANGED:<fields>`, and no score at all rather than a low one). **Residual:** `model_revision` is only what a provider echoes back, and no model wired here publishes one — so a silent provider-side swap behind a stable model id whose output dimension is unchanged is still undetectable. Everything detectable locally is now detected. `capability_profile_version` was deliberately not stored: its only failure mode independent of the fields above is a changed declared dimension, which `dimension` already catches. | `core/style/style_core/space.py` |
| 2.20 | **Fixed 2026-08-25 — a duplicate generate request replays instead of 409ing.** The timeline fence is evaluated under the Shot's row lock, so the loser of a race reads the Shot only after the winner has committed — and reads it `QUEUED`. The fence was then stale against a change the loser's own duplicate caused, and the request was told `shot or authoritative timeline binding changed; plan the shot again` for work already running, never reaching the `except IntegrityError` replay path that exists for this race. A stale fence is now conclusive only once the idempotency key is known to be unclaimed: if a claim exists it is a claim for this same request (`replay` still refuses a key whose request hash differs) and the idempotent answer is the competitor's job. Ordering the claim ahead of the fence would reach the same place through `IntegrityError`, but would mean writing a job row before the plan behind it has been validated. Two regression tests: the threaded one that exposed it, and a deterministic `postgres_only` one that opens the window directly. Both fail if the handler is removed. | `services/generation-gateway/generation_gateway/gateway.py` |
| 2.21 | **Fixed 2026-08-25 — eight integrity guards raised the wrong SQLSTATE on PostgreSQL.** The asset-registry and project-style plpgsql guards raised with no ERRCODE, so PostgreSQL reported `P0001` and SQLAlchemy raised `ProgrammingError`, while the identical SQLite guards raised `IntegrityError`. `except IntegrityError` therefore caught on the development engine and not on the production one. Migration `0039_integrity_errcodes` replaces both functions with `USING ERRCODE = '23514'`; `models.py` matches for the `create_all` path; a test now fails if any plpgsql guard omits its SQLSTATE. The character-state head fence keeps `40001` deliberately — a stale fence means retry, not invalid data — and its test accepts either class with the same message. | `migrations/versions/0039_integrity_errcodes.py` |
| 2.22 | **Fixed 2026-08-25 — `create_all()` could not build the schema on PostgreSQL at all.** `enforce_payment_ledger_append_only()` was declared through SQLAlchemy's `DDL` construct with a single `%` in `RAISE EXCEPTION '% is append-only'`; `DDL` percent-interpolates its statement, so it raised `TypeError` before the trigger could be created. Latent because `create_all` had never been run against PostgreSQL — the migration path uses `op.execute`, which does not interpolate, so production was never affected. | `packages/domain/production_domain/models.py` |
| 2.23 | **Narrative memory compares vectors on a narrower space than the style gate does.** Retrieval filters candidates by `embedding_provider`, `embedding_model` and `embedding_dimension` before scoring (`core/memory/memory_core/engine.py`), so a model swap or dimension change cannot silently mix spaces — but normalization, distance metric and model revision are not part of that filter, and a `ShotMemory` row carries no equivalent of `EmbeddingSpaceIdentity`. Narrower than the hole §2.19 closed, and the same shape. | `core/memory/memory_core/engine.py` |
| 2.24 | **The starter grant covers exactly one generation, at the default duration only.** Every workspace is created with 50 credits. Seedance 2.5 — the model a FREE workspace is routed to — quotes 44 credits at the 4s Passenger default, 54 at 5s and 87 at 8s; Flow Veo 3.1 at 8s quotes 192. So the grant buys one short clip and nothing else, and any request longer than the default is refused before a Job exists. Pre-existing and unchanged by §2.17 — FREE was always charged — but now reachable for every tier that has not topped up, and the paid tiers have no grant of their own beyond the same 50 (only a DePay purchase adds 3,000). Whether that is right is a pricing decision, not an engineering one. | `core/entitlements/entitlement_core/credits.py`, `core/cost/cost_core/service.py` |
| 2.25 | **Updated 2026-08-27 — the counts moved; the substance did not.** The registry now holds 24 canonical models, 14 of them generative (11 video, 3 image). `grok-video-official` and `veo-3.1-quality-official` were retired as duplicates, and their evidence rows are kept with `lifecycle: RETIRED` plus a `superseded_by` naming the OpenRouter route that took over — execution and provenance being different facts. `wan-3.0-openrouter` joins with no public evidence at all. Original text: **Ten of the twelve generative models this platform runs have no diagnostic external evidence.** Recorded by the External Evidence Registry (`external-evidence-v1`). Only `veo-3.1-fast-openrouter` (OSCBench, exact variant) and `gpt-image-2-openrouter` (Qwen-Image-Bench, PhyEditBench, BizGenEval) have per-dimension public backing. `wan-2.7`, `kling-3-pro`, `kling-3-std`, `veo-3.1`, `veo-3.1-lite` and `flow-veo-3.1` have a holistic Arena preference reading and nothing else. `seedance-2.5-official` has 31 metrics on file and **zero** eligible, because all of them belong to Seedance 2.0. `seedream-5.0`, both Grok video models, `flow-narwhal-image` and `veo-3.1-quality` have no public evidence at all. Every `capability_prior` in `config/model-registry/defaults.json` therefore remains hand-authored judgement; the registry's job today is to say which numbers those are. `GET /internal/models/external-evidence` reports it. | `config/external-evidence/registry-v1.json` |
| 2.26 | **Resolved 2026-08-27 — a 30s route exists again.** `wan-3.0-openrouter` (`alibaba/wan-3.0`) is registered and enabled and declares 30s, so a long-form shot no longer fails routing with `DURATION_UNSUPPORTED`. It reaches the model through OpenRouter, which needs no Beta invitation; `wan-3.0-official` stays disabled for want of DashScope access. Original text: **The duration ceiling is back at 15s.** `wan-3.0-official` was the only model declaring 30s, and it is disabled because this account has no Wan 3.0 API access. A shot longer than 15s now fails routing with `DURATION_UNSUPPORTED` before a Job exists — correct, and asserted by a test, but it means long-form shots have no route until either Wan 3.0 access arrives or another 30s model is registered. | `config/model-registry/defaults.json` |
| 2.27 | **The router's duration gate cannot express a request-dependent ceiling.** `VideoModelRouter` reads one `max_duration` per profile, and Wan 2.7's depends on the request: 15 seconds normally, 10 when the shot carries a reference video into R2V. The registry records the exception (`modes.r2v.max_duration_with_reference_video`) and the adapter enforces it per request, so a 12-second reference-video shot fails closed with `INVALID_REQUEST` **before** anything is billed — but it fails at the adapter rather than being excluded at routing, where `ShotRequirements` already carries both `duration` and `requires_reference_video` and could have decided it. Expressing it generically needs a new profile field, which is a migration. | `core/model-registry/model_registry_core/router.py` |
| 2.28 | **I2V's material-combination table is held in the adapter, not the profile's capability flags.** Wan 2.7 I2V publishes a closed list of valid media sets — a last frame needs a first frame or a first clip beside it, driving audio needs something to drive. `_I2V_COMBINATIONS` enforces it and `test_wan_adapter_bounds_match_the_registry_declaration` pins it against `modes.i2v.material_combinations`, so the two cannot drift — but the router's capability flags are per-axis booleans and cannot represent "these axes only in these combinations". A shot asking for an impossible combination is refused before billing rather than excluded from routing, the same shape as §2.27. | `providers/wan/wan_provider/adapter.py` |

| 2.29 | **No calibration bridge exists between any external scale and any production outcome, so no external evidence reaches the production posterior.** All 112 eligible external priors from the 2026-08-26 research are refused with `NO_CALIBRATION_BRIDGE`. This is the isolation rule working rather than a defect — a VBench 0.939 and a production acceptance rate are not two readings of one quantity — but it does mean the three external layers are, today, purely a reporting surface. Building a bridge means measuring the same models on both scales over overlapping material and publishing the mapping; `calibration.BRIDGES` is the one place an entry would go, and it carries an anchor count so a two-point line cannot masquerade as a calibration. | `core/router-evidence/router_evidence_core/calibration.py` |
| 2.30 | **`qc_prompt_alignment` is recorded, given a posterior, and cannot affect routing.** The router publishes fifteen capability dimensions and prompt adherence is not one of them, and the evaluator publishes fourteen named checks and prompt adherence is not one of those either. Both gaps are real; neither is filled by mapping the outcome onto `visual_quality` or the check onto `scene`, which would move a score for a reason nobody could reconstruct. Closing it means adding a dimension to `ModelCapabilityProfile.capability_prior` and a check to `CHECK_NAMES`, in that order. | `core/router-evidence/router_evidence_core/observations.py` |
| 2.31 | **`exact_version` is the registry profile version, which pins our configuration and not always the provider's weights.** For `doubao-seedance-2-5-260628` the model id carries a dated snapshot and the pair really does identify the weights; for `google/veo-3.1` the id is an alias the provider may repoint, and all the profile version pins is our side. `model_is_alias` exists for that case and quarantines the observation rather than attributing it, but **nothing currently sets it** — no provider here publishes a resolved snapshot in its response for the adapter to compare against. A silent repoint behind a stable alias would therefore pool outcomes from two different models. Detectable only if a provider starts reporting what actually ran. | `core/router-evidence/router_evidence_core/observations.py` |
| 2.32 | **Replay compares policies on context buckets, which is the direct method and carries its assumptions.** Only the arm that actually ran has an outcome for a given shot, so within a bucket — task, scene, duration bucket, resolution, reference mode — every model with observations gets an empirical mean and a policy scores the arm it picked. A model that was only ever chosen for the easy shots inside a bucket will look better than it is. The bucket definition is the mitigation and `unscored_contexts` is the honesty check; a doubly-robust estimator would need logged propensities, which the router does not produce because it is deterministic. | `core/router-evidence/router_evidence_core/replay.py` |

| 2.33 | **Ark and DashScope media hosts are still unlisted, so their generations still fail after being billed.** `PROVIDER_MEDIA_ALLOWED_HOSTS` gained `openrouter=openrouter.ai,*.openrouter.ai` on 2026-08-27, read from a real completed OpenRouter job. Ark and DashScope were deliberately **not** guessed: a wrong host is either a hole in the SSRF fence or the same silent failure again. Until one live generation on each shows what host their finished artefact actually comes from, a Seedance or Wan clip still completes at the provider, gets billed, and dies at the fetch. The refusal now names the host it saw, so a single canary per provider closes this — but a canary costs money, so it is your call when. | `packages/shared/platform_shared/config.py` |
| 2.34 | **The local `api` container image is not rebuilt by anything and drifts behind the migration chain.** It runs a built image (`build: .`), not mounted source — only a media-cache volume is mounted — so `git pull` and merges change nothing for it. On 2026-08-27 it was five migrations stale and crash-looped with `Can't locate revision identified by '0051_token_pricing'`, which reads exactly like a bad merge and is not one. `docker compose build api && docker compose up -d api` after any migration lands. Nothing enforces it. | `docker-compose.yml` |
| 2.35 | **Five models are deliberately unpriced after `0051`.** The migration priced the twelve per-token models from vendor documentation and left the rest alone rather than guessing: `grok-imagine-video` on the OpenRouter route among them. An unpriced model refuses to quote rather than quoting a made-up number, which is the intended behaviour — but it means those models cannot be used until someone reads their published rate. | `migrations/versions/0051_token_pricing_profiles.py` |
| 2.36 | **The PostgreSQL half of the test matrix cannot be run as a foreground tool call.** It takes ~11 minutes; the tool timeout caps at 10, so the run is SIGKILLed at exit 137 partway through and looks like a crash. pytest peaks at ~600 MB RSS, there are no jetsam entries and the Postgres container never dies — it is a harness limit, not resource pressure. Run it detached (`nohup … & disown`) and poll for the exit code. Costs nothing to work around, wastes an hour if you do not know it. | — |
| 2.37 | **Adopted generation output keeps its `staging/generation/…` key for ever, so the staging listing grows with the media plane.** Added 2026-08-28, deliberately: adopting the staged object in place is what makes a rolled-back completion leave *only* recyclable staging objects, with no copy step to crash inside. The cost is that `sweep_generation_staging` lists every adopted object on every run and keeps them as `kept_referenced` — correctness is untouched (the sweep is chunked, so kept keys cannot starve deletable ones), but at large media volumes the hourly listing gets linearly slower. If it ever matters, the options are a date-partitioned key scheme the sweeper can skip wholesale, or a post-adoption promotion job that moves objects to content-addressed keys and updates the row — the second reintroduces a two-step mutation and should not be done casually. | `services/media-service/media_service/staging.py` |

## 3. Incomplete work

| # | Item | Blocked by |
| --- | --- | --- |
| 3.1 | **Model-backed prompt compilation.** `compile_input()` is deterministic; `skill_contract()` never calls a model. Steps are in `HANDOFF.md` §18. | §1.7 decision |
| 3.2 | **Live provider evidence.** Wan 2.7 **T2V is verified live** — submitted, polled, `COMPLETED` with a `video/mp4` artefact on 2026-08-25. I2V and R2V remain unverified because both carry a reference and `S3_*` is still unset (§1.3). Image, chat and embedding roles have never been called. | §1.3 for I2V/R2V |
| 3.3 | **Omni reference file** in `skills/model-prompting/references/`. Wan 3.0's 30s envelope, the Seedance 2.5 entry and a GPT Image 2 entry have landed. | §1.5 |
| 3.4 | **A client that uses the direct-upload endpoints.** The server side is complete (`POST /v1/assets/uploads` + `/complete`); the Web UI still posts multipart to `POST /v1/assets`. Nothing is broken — the streaming path remains — but the benefit only arrives once the client performs its own PUT. | — |
| 3.6 | **The router-evidence loop has no production data yet.** Everything is built and green — contract, table, recorder, posterior, replay, LCB, exploration simulator — and `router_observations` has zero rows because the recorder landed on 2026-08-26. Until it has 20, `scripts/router_posterior_run.py` exits 2, no replay exists, and the LCB flag has no evidence it could be enabled on. Not blocked on anything an agent can do. | §1.15 |
| 3.5 | **No live evidence for the semantic style layer.** `google/gemini-embedding-2` has never been called; the tests use a deterministic stub. Its real vector geometry — and therefore the right value for `semantic_similarity_threshold`, currently 0.80 — is uncalibrated. | §1.1 gate |

## 4. P2 — release blockers

- **Partly closed 2026-08-25.** `0035`–`0039` now have a populated PostgreSQL upgrade, downgrade and
  re-upgrade, run against the compose database at `0027` holding 13 `model_definitions` rows — the first
  populated migration evidence this project has on PostgreSQL rather than on an empty temporary database.
  Still missing: a *production-shaped* dataset, and a backup/restore drill. Neither is a migration test.
- **The repository has no remote.** Everything, including commit `ea9d042`, exists only on this
  machine. A disk failure loses the entire project. This is the cheapest unaddressed risk on
  the list.
- No live evidence for Payment, Provider, VLM, real billing or real on-chain payment.
  Known spend remains **USD 0**.
- Email verification, MFA, member/device sessions, production secrets/HTTPS, backup and
  restore, monitoring and alerting, and operational process all still block release.
- Production visual detector / tracker / encoder and a trusted `VLM_REVIEWER` are neither
  deployed nor calibrated.

---

## 5. Closed on 2026-08-23

| Previously | Outcome |
| --- | --- |
| Wan 2.7 dropped every reference image it was given | `_video_payload` read a reference *video* and a first frame and nothing else, so `reference_images`/`reference_urls` — the list the Gateway resolves, and pays to resolve — never reached DashScope. A shot generated on four character plates rendered as if it had none. Inputs are now carried in `input.media[]` with an explicit role each. |
| A first clip, a reference video and a reference image were the same thing | Three roles now, distinct end to end: `first_clip` (continuation), `reference_video` (R2V reference) and `reference_image`. `common_payload` carries `first_clip` separately, and a shot whose inputs no Wan mode carries is rejected before it is billed rather than half-sent. |
| Voice reference had no way to be declared or refused | `supports_audio` means native audio **out** — the router reads it for `requires_native_audio`. There was no flag for an audio asset carried **in**, so a profile could imply voice conditioning no adapter could send. `supports_reference_voice` is now its own capability (migration `0038`), `false` for Wan 2.7, and a request carrying a voice reference fails closed. The serializer can express one — `{"type": "audio", "url": …}`, still only the two official fields — so the flag is the gate, not the wire. A drift gate asserts the two cannot disagree in either direction. |
| `first_clip` had no mode and failed closed | Restored to I2V, which is where a clip the shot grows out of belongs — the same operation a first frame performs. `supports_video_extension` is `true` again. Continuing from footage and referencing it now select different models (I2V vs R2V), and asking for both is refused rather than silently resolved to one. |
| R2V was modelled as refusing a first frame | Corrected against the deployment documentation: R2V's matrix is `first_frame` **together with** `reference_image`/`reference_video`, and that combination is now what selects R2V. It had been rejected as inexpressible, which is the opposite of the mode's purpose. |
| `media[]` entries carried a `role` field | The wire contract is `type` and `url` only. The role is canonical internal state — it picks the model, enforces the bounds and orders the array — and is dropped at the boundary. Because the provider sees no role, array position is the only signal, so ordering became part of the contract. |
| `parameters.size` carried `"720p"` | No `size` is sent at all: it is not in the published parameter set. Framing is `resolution` plus, where nothing else fixes the aspect, `ratio` — both for T2V, `resolution` alone for I2V, and `ratio` on R2V only without a first frame. A caller asking for pixel dimensions is refused rather than silently ignored. |
| `max_reference_images: 4` was a guess | Replaced with the published bounds — one first frame, five reference assets (images and videos together) — enforced in the adapter before billing and asserted against the registry by a drift gate. |
| Wan 2.7's profile blurred continuation, reference and edit into `supports_v2v` | `wan-2.7-manual-v3` separates them: `supports_video_extension` for continuation, `supports_v2v`/`supports_reference_image`/`supports_multi_reference`/`supports_character_reference` for reference, and an explicit `edit: supported=false`. The mode→role matrix is recorded in `provider_metadata` so the registry and the adapter cannot drift. |
| Completing one upload twice could under-count storage | The upload row is locked and exactly one caller leaves `PENDING`; the loser gets the winner's asset instead of settling a second time. Adopt, completion and settlement now share one transaction, so a crash between them rolls back to a `PENDING` upload with its hold intact. |
| A lost authorize race surfaced as a 500 | The read and the insert are separate transactions; a colliding insert is now recognised as the replay it is. Workspace-backed projects were serialized by the reservation's unique constraint, but a project with no workspace had no such guard. |
| The lineage key had two definitions | One, in `media_service.lineage_key`. Completion also consumes the value the authorization stored on the upload row instead of deriving it again — that derivation was the second formula. |
| Wan 2.7 posted `wan2.7-t2v` / `wan2.7-i2v` | Replaced with the dated runtime IDs: `wan2.7-t2v-2026-06-12`, `wan2.7-i2v-2026-04-25`, `wan2.7-r2v-2026-06-12`. `.env` and `.env.example` carry all three. |
| Only `supports_t2v` was declared for Wan 2.7, so I2V and R2V were unroutable | `wan-2.7-manual-v2` declares i2v, v2v, start frame, reference images and audio. The adapter's I2V and R2V paths were reachable only by an explicit request before this; the router could never choose them. |
| The adapter defaulted Wan 3.0 to `wan3.0-video` | Removed. Wan 3.0 is invitation-only Beta with no reviewed runtime ID, so it fails closed (§1.9) instead of posting a guess. |
| A failed `POST /v1/assets/uploads` kept the workspace's storage hold | Released, and only a hold *that call* created. The hold was taken before the presign, so every failure after it — including the `501` that local disk returns on **every** attempt today — permanently consumed capacity and left the Idempotency-Key answering "upload is already in progress" forever. |
| Re-authorizing an upload was refused for any workspace-backed project | The route now looks the upload up before reserving and reuses the hold it already owns. `WorkspaceStorageQuota` reads a `RESERVED` row as "already in progress", which is right for a multipart upload passing through this process and wrong for one the client holds its own presigned URL for — so `DirectUploadService`'s replay path was unreachable in production. |
| `POST .../complete` before the client's PUT landed abandoned the session | A missing object is now terminal only once the window has closed. Polling early used to mark the row `ABANDONED` and release the hold while the presigned URL was still live, so the bytes the client then wrote had no row left to adopt them. A transient `read_prefix` failure had the same effect. |
| A completion failure on a workspace-less project left the row `PENDING` forever | `abandon()` no longer depends on a reservation existing. |
| §1.1 `RUNAPI_BASE_URL` / `RUNAPI_MODEL_ID` empty | Configured (`https://runapi.ai`, `gpt-5.6-luna`). `RUNAPI_IMAGE_PATH` and `RUNAPI_VIDEO_PATH` were missing from `.env` and are now present. |
| §2.1 Style lock injection is fragile | Enforcement moved into `PromptCompilerService`, which resolves the project lock from `ProjectStyleService` itself. A caller-supplied `style_lock` can no longer override the authoritative one. `scripts/simulate_short_story.py` no longer mirrors the old injection and is now the end-to-end proof. |
| §2.2 `MemoryQuery` has no `episode_id` / recency weighting | `episode_id`, `EpisodeScope` (`EPISODE`/`SERIES`) and `recency_half_life_days` added. Scoping is now per layer: L0 series-wide, L1 scene-fenced, L2 episode- or series-scoped. Episodic recall was previously fenced to the current scene, which made the layer unable to see anything it exists to recall. |
| §2.6 `DELETE /videos/{id}` is not an OpenRouter endpoint | `cancel_job` now reports `False` instead of calling it. A separate defect was found alongside: a completed OpenRouter video publishes its artefact in `unsigned_urls`, which the adapter never read, so a finished billed generation could only report `OUTPUT_URL_MISSING`. Both are covered by tests. |
| §2.7 `VIDEO_GROK` / `VIDEO_VEO` FALLBACK on transportless stubs | Both bindings removed. The model definitions stay registered — the router and capability resolver read them as capability records — and `test_model_routing_integrity.py` now rejects a binding of *any* kind onto a stub provider, not just a PRIMARY. |
| §3.4 Episode-scoped retrieval | Landed with §2.2; `prepare_autopilot` now retrieves with `episode_scope=SERIES` against the shot's own episode. |
| §1.10 Batch images were loose project media | `image_count` is an explicit opt-in, the whole batch is priced and reserved before the call, and images 2..n each become their own `GenerationCandidate` on the shot. |
| Reference media resolved to this service's authenticated route | `StorageProvider.presigned_reference_url()` issues a short-lived credential from object storage. Local disk returns `None` and generation fails closed rather than proxying. The previous behaviour would have returned 403 to every provider *and* put the API in the media path. |
| A provider's upload cap implied downscaling the original | `media_renditions` (`0035`). Originals are immutable; derived copies are lazy, cached, and keyed by the constraints that caused them. |
| Paid image admission hard-coded `google_flow`/`NARWHAL` | Resolves the `IMAGE_GENERATION` role, like video. |
| §3.4 Uploads streamed through the API | `POST /v1/assets/uploads` issues a presigned PUT whose SHA-256 the object store enforces; `/complete` adopts the object from a `HEAD` plus a 64 KB header read. Migration `0037`. Local disk answers `501` and names the multipart endpoint. |
| Layer 2 disabled | `FEATURE_SEMANTIC_STYLE_LOCK=true`. A lock that cannot obtain its semantic reference now records `style_layers` and `semantic_layer_absent_reason` instead of being silently single-layer. |
