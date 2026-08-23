# Open Issues — everything left unresolved

Snapshot: 2026-08-23 · commit `ea9d042` · companion to [`../HANDOFF.md`](../HANDOFF.md)

Every item this session raised and did **not** finish, in one place. Section 1 needs you.
Sections 2–4 are engineering work and need no decision from you.

Items closed on 2026-08-23 are listed in section 5 rather than deleted, so a reader of the
previous snapshot can see where each one went.

---

## 1. ACTION REQUIRED FROM YOU

Nothing below can be done by the agent — each is either a cost decision, a credential, or a
product judgement.

### 1.1 Live calls are still gated off

All three must be true together, in `ai-director-platform/.env`:

| Variable | Now | Must be |
| --- | --- | --- |
| `PROVIDER_MODE` | `mock` | `live` |
| `ALLOW_LIVE_PROVIDER_CALLS` | `true` ✅ | `true` |
| `LIVE_PROVIDER_CONFIRMATION` | `I_UNDERSTAND_THIS_COSTS_MONEY` ✅ | same |

Two of the three are already set. **`PROVIDER_MODE` is the only one left**, and the agent will
not flip it for you: it is the switch that turns every provider transport from a network-free
mock into a billable HTTP client.

A live *generation* additionally needs a `LiveCanaryPermit`, created through
`POST /internal/live-canary-permits` (requires `PLATFORM_API_KEY`, currently empty).

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

`S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY` are empty, so the storage
backend is local disk and cannot issue a presigned URL. Seedance, Wan, OpenRouter and RunAPI
fetch reference media **themselves, from object storage**; the API is deliberately not in that
path. Until a bucket is configured, every reference-carrying shot — and every image *edit* —
fails closed with `PROVIDER_REFERENCE_URL_UNAVAILABLE`.

`LOCAL_REFERENCE_SIGNING_KEY` is set in `.env` so local development can exercise edits. That
route **does** proxy bytes through this service. It is a development affordance: leave it empty
in production. Also set `PUBLIC_BASE_URL` to an HTTPS origin.

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

Ark, DashScope and RunAPI keys were pasted into the conversation transcript. Your earlier
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

### 1.9 Optional: promote Wan 3.0

`VIDEO_WAN` primary is Wan 2.7 per your kernel spec, with 3.0 as an explicit fallback. Wan 3.0
has a 30-second native single-shot envelope, which is the direct fix for the compressed-shot
character fragmentation you described. Promoting it is your call. The envelope is now recorded
in `skills/model-prompting/references/wan.md` either way.

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
| 2.11 | Abandoned direct uploads leave an object in the bucket. A client that authorizes, PUTs, then never completes has its quota hold expire, but nothing sweeps the orphaned object or the `PENDING` row. | `services/media-service/media_service/direct_upload.py` |
| 2.5 | `configure_runtime_model` reconciliation only runs for models created by *this* startup's default sync, so adding a credential to `.env` later does not re-enable a model that was disabled for want of one. Deliberate — startup must not replay defaults over an administrator's changes — but there is no operator path to re-enable besides the registry API. | `apps/api/video_platform_api/container.py` |

## 3. Incomplete work

| # | Item | Blocked by |
| --- | --- | --- |
| 3.1 | **Model-backed prompt compilation.** `compile_input()` is deterministic; `skill_contract()` never calls a model. Steps are in `HANDOFF.md` §8. | §1.7 decision |
| 3.2 | **Live provider evidence.** No provider has been called. The image path now has a one-command opt-in route (§1.2); video, chat and embedding roles do not. | §1.1 gate |
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
