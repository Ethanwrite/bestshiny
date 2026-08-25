# Open Issues — everything left unresolved

Snapshot: 2026-08-25 · live gate OPEN · Wan T2V verified · companion to [`../HANDOFF.md`](../HANDOFF.md)

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

**It is invitation-only Beta**, so no runtime model ID has been reviewed for it and the adapter
no longer carries a guessed default (`wan3.0-video` was never verified against DashScope). A
shot routed to 3.0 now fails closed with the reviewed-model error rather than posting an unknown
ID. When your invitation arrives, one line turns it on:

```
WAN_VIDEO_MODEL_KEYS=wan-3.0=<the model ID DashScope issues you>
```

The model definition and its FALLBACK binding stay registered meanwhile — the router and the
capability resolver read them as capability records, exactly as `VIDEO_GROK` and `VIDEO_VEO` do.

### 1.10 Layer 2 is on, but lock your styles *after* going live

`FEATURE_SEMANTIC_STYLE_LOCK` is now `true` and `STYLE_SEMANTIC_EMBEDDING` resolves to
`google/gemini-embedding-2`. **It cannot run yet**: while `PROVIDER_MODE=mock` the embedding
call reaches no model, so a lock made today carries a single layer and records why in
`metadata_json.semantic_layer_absent_reason`.

`ProjectStyleLock` is append-only and a database trigger forbids re-locking, so **a style locked
before layer 2 can run keeps the single gate permanently.** If the two-layer gate matters for a
project, do not lock its style until `PROVIDER_MODE=live`.

Two consequences of the layer itself, now that it is enabled:

- **Cost.** One `google/gemini-embedding-2` call per locked style, and one per evaluated
  candidate. Roughly USD 0.00045 per image, so a 60-episode series at a few hundred candidates
  is cents, not dollars — but it is a per-candidate paid call on the commit path.
- **Meaning.** It changes what "committable" is. A candidate that layer 1 passes can now fail
  on rendering medium, and a candidate whose semantic evidence is unavailable becomes
  `REVIEW_REQUIRED` rather than passing. That is the intended behaviour — a missing second
  opinion is not a passing one — but it means an outage at the embedding provider turns into a
  review queue rather than silent approval.

`semantic_similarity_threshold` defaults to 0.80 and is uncalibrated — the model has never been
called, so the real distribution of its similarity scores is unknown. Expect to tune it against
the first live batch rather than trusting the default.

---

## 2. Known defects — not fixed

| # | Defect | Where |
| --- | --- | --- |
| 2.1 | Retrieval is keyed on the current shot's prompt text, so narrative dependency is invisible to it. The ledger covers obligations and retrieval can now reach across episodes, but *which* earlier beat matters is still decided by similarity alone. | `services/production-engine/production_engine/runtime.py` |
| 2.2 | Aggregate style drift across episodes is unmonitored — each shot can pass while the series slowly walks away from episode 1. | `core/style/style_core/service.py` |
| 2.3 | `timeline_scope_key` branch proliferation (flashback/dream) has no merge or retirement policy. | `core/character/` |
| 2.4 | A synchronous provider's result is held in the Gateway process between the confirmed submission and the poll that consumes it, both inside one `process()` call. Process death in that window loses the artefact. It is **not** a silent success or refund — the poll then fails with `OPENROUTER_IMAGE_RESULT_NOT_RETRIEVABLE` while `submitted` stays true, so the credit moves to `RECONCILIATION_REQUIRED`. Making it durable needs a migration. | `services/generation-gateway/generation_gateway/gateway.py` |
| 2.6 | `MediaRenditionKind.THUMBNAIL` exists in the schema and nothing generates one. The Web UI still reads originals, so a gallery of 4K plates downloads 4K plates. | `services/media-service/media_service/renditions.py` |
| 2.7 | Derived renditions are never retired. They are content-addressed and bounded, but a provider that repeatedly changes its limits accumulates one copy per constraint set with no garbage collection. | `services/media-service/media_service/renditions.py` |
| 2.8 | The batch-to-candidate path allocates sibling candidates in a short transaction before the media is registered. If the process dies between the two, the shot keeps empty `CREATED` candidates. They are inert — no output asset, so nothing can commit them — but nothing sweeps them either. | `services/generation-gateway/generation_gateway/gateway.py` |
| 2.9 | Video is never adapted to a provider's reference constraints; an over-large video reference fails closed instead of being transcoded. Correct today, but it means video-reference providers need originals inside their limits. | `services/media-service/media_service/renditions.py` |
| 2.10 | A direct upload is validated from a bounded 64 KB header, not a full decode. Magic bytes, declared format and pixel dimensions are checked; a **truncated or internally corrupt** file passes and fails later at first use, where `RenditionResolver` raises `RenditionDerivationFailed`. Deliberate — pulling every upload back through the API to catch it one step earlier would undo the reason writes bypass the API — but it is a real difference from the multipart path. | `packages/shared/platform_shared/media_validation.py` |
| 2.11 | `POST /internal/maintenance/expired-uploads` now reclaims an upload that was authorized and walked away from, but **nothing schedules it** — it is an endpoint an operator or cron must call. The object itself is never deleted: removing bytes a user may have paid to upload is not a sweeper's decision, so an abandoned upload still leaves one orphan in the bucket. Stale `RESERVED` holds with no `PENDING` upload behind them are reported for reconciliation and deliberately not released, because a hold whose registration succeeded and whose settlement failed is a fail-closed hold that must survive. | `apps/api/video_platform_api/main.py` |
| 2.12 | **Savepoints do not roll back under pysqlite.** Work inside `session.begin_nested()` survives a rollback of the enclosing transaction — verified directly: a plain insert rolls back, the same insert inside a savepoint does not. Seven call sites still rely on savepoints (`registry.py` ×2, `renditions.py`, `gateway.py`, `affinity.py` ×3); each of them silently loses its rollback guarantee whenever `DATABASE_URL` is SQLite, which is the **default**. The direct-upload completion path was rewritten to need no savepoint, so it is correct on both engines. The documented pysqlite workaround (`isolation_level = None` plus an explicit `BEGIN`) was tried and reverted: it makes every transaction take a write lock, and the concurrency suites either fail on `database is locked` or hang. The real fix is PostgreSQL, or per-call-site restructuring like the one done here. | `packages/database/platform_database/session.py` |
| 2.13 | The R2V `ratio` condition is an inference, not a quoted rule. The documentation gives "R2V → resolution + conditional ratio" without naming the condition; this adapter sends `ratio` only when no first frame is supplied, on the same logic that makes I2V take no ratio at all — a supplied frame already fixes the aspect, and sending both asks one question twice. Worth confirming against a live R2V call. | `providers/wan/wan_provider/adapter.py` |
| 2.14 | `supports_reference_voice` is now a first-class capability on **every** model profile and is `false` everywhere, including models whose provider may well accept a voice reference. Only Wan 2.7 was actually reviewed for it; the rest inherit the column default. A model that does accept one is currently under-declared rather than mis-declared — it fails closed — but the flag should be reviewed per provider rather than assumed. | `config/model-registry/defaults.json` |
| 2.15 | **P0 — the local database cannot be migrated forward.** `data/platform.db` is stamped `0020_provider_media_upload_claim`, eighteen revisions behind head, yet it holds tables from far later revisions. `Database.create_all()` runs on every startup and creates *missing tables* from ORM metadata; it never adds columns to tables that already exist and never advances the alembic stamp. The result is a hybrid no migration can repair: `alembic upgrade head` dies immediately on `0021_unified_model_registry` with `table model_definitions already exists`, while `model_capability_profiles` still lacks `supports_image_generation` and everything added after it. Reproduced on a copy. The root cause is two schema authorities — `create_all()` and alembic — running against one database. | `packages/database/platform_database/session.py` |
| 2.16 | Importing `video_platform_api` builds a container as a side effect: `__init__.py` imports `main`, and `main.py` ends with `app = create_app()` at module scope. Any import — a script, a test collection, a linter plugin — therefore opens `DATABASE_URL`, runs `create_all()` and seeds defaults. It is why the live Wan test, which touches no database, could not run until it was pointed at a scratch one. | `apps/api/video_platform_api/main.py` |
| 2.5 | Startup still never replays defaults over an administrator's changes, which is right. `POST /internal/models/reconcile-live` is now the operator path that was missing: it re-derives `live_enabled` for every model from the credentials present, reports by default and writes on `?apply=true`. It moves only enablement — never the execution ID — so it cannot overwrite an administrator's chosen model. **Nothing calls it automatically**, so adding a credential still requires that call. | `apps/api/video_platform_api/main.py` |

## 3. Incomplete work

| # | Item | Blocked by |
| --- | --- | --- |
| 3.1 | **Model-backed prompt compilation.** `compile_input()` is deterministic; `skill_contract()` never calls a model. Steps are in `HANDOFF.md` §8. | §1.7 decision |
| 3.2 | **Live provider evidence.** Wan 2.7 **T2V is verified live** — submitted, polled, `COMPLETED` with a `video/mp4` artefact on 2026-08-25. I2V and R2V remain unverified because both carry a reference and `S3_*` is still unset (§1.3). Image, chat and embedding roles have never been called. | §1.3 for I2V/R2V |
| 3.3 | **Omni reference file** in `skills/model-prompting/references/`. Wan 3.0's 30s envelope, the Seedance 2.5 entry and a GPT Image 2 entry have landed. | §1.5 |
| 3.4 | **A client that uses the direct-upload endpoints.** The server side is complete (`POST /v1/assets/uploads` + `/complete`); the Web UI still posts multipart to `POST /v1/assets`. Nothing is broken — the streaming path remains — but the benefit only arrives once the client performs its own PUT. | — |
| 3.5 | **No live evidence for the semantic style layer.** `google/gemini-embedding-2` has never been called; the tests use a deterministic stub. Its real vector geometry — and therefore the right value for `semantic_similarity_threshold`, currently 0.80 — is uncalibrated. | §1.1 gate |

## 4. P2 — release blockers

- Migration head `0037_direct_uploads` has offline/temporary-database evidence only.
  Production-shaped populated upgrade and rollback are unverified across `0035`–`0037`.
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
