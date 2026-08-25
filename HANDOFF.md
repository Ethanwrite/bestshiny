# AI Director Platform — Handoff

Date: 2026-08-23 · Branch `main` · commit `ea9d042` · **NOT PRODUCTION-READY** · no remote

This is the single current entry point. It supersedes the 2026-08-20 and 2026-08-22
development handoffs and the Visual Runtime implementation record, all three deleted
because they described states the code no longer has.
Architecture truth lives in [`CURRENT_ARCHITECTURE.md`](CURRENT_ARCHITECTURE.md).

## 1. Gate state (all green, offline only)

```
.venv/bin/python -m pytest -q       643 passed, 5 skipped, 62 warnings
.venv/bin/ruff check .             All checks passed
.venv/bin/python -m mypy           Success: 133 source files
.venv/bin/python -m alembic heads  0038_reference_voice_capability (single head)
```

The 5 skipped are the opt-in live tests; they need `--run-live-provider` *and* the
three-part gate.

**Wan 2.7 T2V is verified live** (2026-08-25). `PROVIDER_MODE=live` on the user's
instruction; task `285f787d-c1fe-40c5-8893-6e1f89adbb70` submitted, polled and
`COMPLETED` with a fetchable `video/mp4` artefact — auth, the `media[]` body, the
DashScope async protocol and the poll parsing all confirmed against the real
service. Known spend is **no longer USD 0**: one 5s 720P clip.

Two things that run cost nothing and were both wrong beforehand:

- `WAN_API_KEY` in `.env` was **truncated** — 73 of 115 characters. DashScope
  answered `Invalid API-key provided`, and that was read here as a revoked key
  rather than a bad copy. The key was always valid.
- The resolution table carried 480P/540P/1440P/2160P as a generic normalisation
  map. Wan accepts `720P` and `1080P` only, exactly as `supported_resolutions`
  in the registry already said. A 480P task was accepted and then failed
  validation at the provider — a wasted round trip for something knowable
  locally. The adapter now refuses it before submission.

Two further gates, both offline:

```
.venv/bin/python scripts/preflight_live.py                      what a live run could and could not do
.venv/bin/python scripts/simulate_short_story.py                3-shot end-to-end, style lock enforced
.venv/bin/python -m pytest --run-live-provider -m live_provider \
    tests/live/test_wan_video_live.py -k "not smallest_t2v"     2 passed — free, no socket
```

`preflight_live.py` is the one to run before flipping the gate. It reads the same `Settings`
the application does, opens no socket, prints no secret, and answers per path: would this
reach the provider, fail closed here, or fail *at* the provider after being billed.

## 2. 2026-08-23 — the image path

The platform had no working image generation. `OpenRouterProvider.generate_image` raised
`CAPABILITY_NOT_SUPPORTED`, no `IMAGE_GENERATION` role existed, and `POST /v1/images/generations`
hard-coded Google Flow's `NARWHAL` in the route handler. `openai/gpt-image-2` is now the
project's image model and the path runs end to end offline.

### What it is

`POST https://openrouter.ai/api/v1/images` — synchronous, text-to-image and image editing,
400K context. The execution envelope is recorded from the model's own capability descriptor
(`GET /api/v1/images/models`, read 2026-08-22) and asserted by a test, so it cannot drift
silently: **10 images per request, 16 reference images**, aspect ratios
`1:1 3:2 2:3 4:3 3:4 16:9 9:16 21:9 auto`, quality `auto low medium high`, background
`auto opaque` — gpt-image-2 publishes no transparent background. A request outside the
envelope is rejected locally, before it can be billed. An image model with no reviewed
envelope is rejected outright, the same fail-closed rule Flow and Wan already follow.

### Synchronous is the whole difficulty

`/images` returns the finished images as base64 in the response body. There is no remote job
and no URL. The Gateway is submit-then-poll, and its completion path downloads from
`output_url`, so neither end matched:

- `ProviderSubmission.result` carries a terminal `ProviderJob` when a provider answered
  synchronously; `ProviderJob.outputs` carries the bytes.
- The confirmation transaction skips the poll delay when a result is already in hand, then
  claims the poll and runs the **existing** completion path — billing evidence, credit
  settlement, candidate/shot status, idempotency, events, canary settlement — unduplicated.
- `MediaRegistry.register_provider_bytes` stores inline bytes through the same content
  validation a downloaded artefact passes. A provider is not a trusted source of decodable
  media.
- Batch images 2..n are registered as project media rather than discarded. The workspace paid
  for them; a candidate may only own one artefact.

The one gap this leaves is recorded as `docs/OPEN_ISSUES.md` §2.4: the result lives in the
Gateway process between confirmation and poll, both inside one `process()` call. Losing it is
**not** a silent success or refund — `get_job` for an image reports
`OPENROUTER_IMAGE_RESULT_NOT_RETRIEVABLE` with `submitted=True`, so the credit moves to
`RECONCILIATION_REQUIRED`. Making it durable needs a migration.

### Where the choice of image model now lives

`ModelRole.IMAGE_GENERATION`, PRIMARY `gpt-image-2-openrouter`, fallbacks `seedream-5.0-ark`
then `flow-narwhal-image-internal`. `POST /v1/images/generations` resolves that role instead of
naming a model, so changing the project's image model no longer means editing a route. An
explicit `provider`+`model` in the request still wins.

Registry defaults are `phase2-model-infrastructure-v3`. `estimated_per_image` is USD 0.1248,
derived from 4160 output-image tokens at USD 0.00003 — quality=high at 1024×1024.

### Two defects found in the same adapter

- `cancel_job` called `DELETE /videos/{id}`, which OpenRouter does not document. Reporting a
  cancellation that never happened frees local capacity while the provider keeps generating and
  billing. It now returns `False`.
- A completed OpenRouter video publishes its artefact in `unsigned_urls`, which the adapter
  never read. A finished, already-billed video could only report `OUTPUT_URL_MISSING`.

### Testing it for real

30 offline tests in `tests/test_openrouter_image_generation.py` cover the allowlist, reference
mapping, envelope enforcement, response parsing, the Gateway completion and the HTTP route.
Live verification is one opt-in command and is described with its cost in
[`docs/OPEN_ISSUES.md`](docs/OPEN_ISSUES.md) §1.2. The gate was checked: with
`LIVE_PROVIDER_CONFIRMATION` unset the run stops before any socket opens.

## 3. 2026-08-23 — the media plane and the second style layer

Four directives, all landed. Each replaced something that worked in development
and would have failed at scale or in production.

### The API is no longer in the media path

Reference media was resolved to `MediaAsset.public_url`, which is
`{PUBLIC_BASE_URL}/v1/storage/{key}` — a route on **this service**, behind
`Depends(auth.current_user)`. Two consequences, one of them fatal:

- an external provider cannot authenticate to it, so live reference edits would
  have returned 403 for every provider;
- had it been open, every reference byte would be read from object storage into
  the API process and streamed out again. A dozen concurrent 4K reference edits
  turns the control plane into an image CDN.

`StorageProvider.presigned_reference_url()` now issues a short-lived credential
from the storage backend itself: a real presign on S3-compatible storage, and
`None` on local disk. `None` fails closed with an error that says to configure
object storage — it never degrades into proxying. Local development keeps a
signed, expiring, opt-in route (`LOCAL_REFERENCE_SIGNING_KEY`) that *does*
proxy, documented as a development affordance rather than the deployment shape.

Reference URLs are computed per submission and never stored: an expiring
credential does not belong in a durable column.

### Original and derivative are separate rows

`media_renditions` (migration `0035`). The user's original is immutable — a
7680x4320 character plate stays 7680x4320, because a face, a product label and a
fabric weave only ever exist at the resolution they arrived at, and a provider's
current upload cap is a fact about that provider.

`GenerationProvider.reference_constraints` declares what a provider accepts;
`RenditionResolver` picks the encoding that fits and derives one lazily when
none does, keyed by a digest of those constraints so lowered limits produce a
new rendition rather than reusing one built for the old ones. Derivation
compresses before it downscales, and refuses below 256x256 rather than shipping
something that no longer carries identity. Under a byte cap it re-encodes a
lossless original to a lossy format on purpose: a 2048px face with mild
compression carries identity a pristine 400px face does not.

Unbounded constraints mean *limits nobody has established*, not an unlimited
provider — the original is sent unchanged rather than guessed at.

### `n > 1` produces candidates, not spare files

Previously batch images 2..n were registered as loose project media. They are
now one `GenerationCandidate` each on the same shot, so a paid batch is a choice
the user makes. `GenerationRequest.image_count` is explicit and opt-in, the
whole batch is priced and reserved **before** the call, and the credit estimate
scales with it — charging for one image and delivering four would make the
workspace balance a fiction. `POST /v1/images/generations` accepts `n` and
returns `image_count` and `estimated_credits`.

The same route stopped hard-coding `google_flow`/`NARWHAL` for paid image
admission; it resolves `IMAGE_GENERATION` like video already did.

### The style lock has a second layer

**Assessment: yes, and it needs no new credential.** `google/gemini-embedding-2`
is GA, natively multimodal, and served through the OpenRouter key already
configured — the same `EmbeddingCapability` Voyage uses. Roughly USD 0.00045 per
image.

The gap is specific. The 64-D descriptor is a histogram of colour, tone,
saturation, edge and spatial statistics. Rendering *medium* barely moves those:
oil paint and a 3D render of the same scene under the same palette score near
1.0, as do 35mm and a phone camera. A series can drift from illustrated to
photographic with every frame passing. The inverse holds too, which is why layer
1 stays: a regrade that preserves the medium reads as "same style" to a semantic
model.

So both run and the **worse** verdict wins — never averaged, because one layer's
confidence must not cover the other's objection. Migration `0036` binds a second
reference embedding to the lock, so the two layers always describe the same
version. Layer 2 unavailable is `REVIEW_REQUIRED`, never PASS: a missing second
opinion is not a passing one, and a project locked under two layers cannot
quietly fall back to one. Off by default (`FEATURE_SEMANTIC_STYLE_LOCK`) because
it is a paid call per candidate and it changes what "committable" means.

## 4. Direct uploads, and layer 2 switched on

### Writes now bypass the API too

Reads went direct in the previous change; writes were the other half. A user
uploading a 38 MB plate still streamed it through the control plane on its way
to a bucket that could have received it directly.

```text
client ──1. authorize──► API              (tenancy, quota hold, key, presigned PUT)
client ──2. PUT bytes──► object storage
client ──3. complete───► API              (HEAD + 64 KB header read, register)
```

`POST /v1/assets/uploads` authorizes and returns a presigned PUT;
`POST /v1/assets/uploads/{id}/complete` adopts the object. The `direct_uploads`
row (migration `0037`) holds everything the server decided, so the completion
call carries only a row id and cannot retarget the upload at another project,
asset type or key.

Two details carry the safety:

- **The store enforces the digest.** `x-amz-checksum-sha256` is bound into the
  presigned PUT, so S3 rejects bytes that do not hash to the declared value.
  Without it the content-addressed key would name content the object might not
  contain. With it, a client-declared SHA-256 is trustworthy without this
  service ever reading the body.
- **Size comes from `HEAD`, not from the client.** A declared size sizes the
  quota hold; the store's number is what gets settled.

Validation reads a bounded 64 KB header — magic bytes, declared format,
dimensions for the decompression-bomb bound. **The full decode the multipart
path performs is deliberately given up**: a truncated file passes here and fails
at first use, where `RenditionResolver` already decodes and raises
`RenditionDerivationFailed`. Pulling every upload back through the API to catch
it one step earlier would undo the point of the change. Recorded as a defect,
not hidden.

Local disk cannot presign a PUT, so `POST /v1/assets/uploads` answers `501` with
the multipart endpoint named. `POST /v1/assets` is unchanged and remains the
path for deployments without object storage.

### `FEATURE_SEMANTIC_STYLE_LOCK` is on

`ModelRoleSemanticStyleEmbedder` is wired and `STYLE_SEMANTIC_EMBEDDING`
resolves to `google/gemini-embedding-2`.

**It does not do anything yet, and that is worth knowing.** While
`PROVIDER_MODE=mock` the embedding call cannot reach a model, so
`ensure_semantic_embedding` returns nothing and a new lock carries a single
layer. That used to be silent. It now records `style_layers` and
`semantic_layer_absent_reason` on the lock, so an accidentally single-layer lock
is distinguishable from a deliberate one.

This matters more than it sounds: `ProjectStyleLock` is append-only and
re-locking is forbidden by database trigger. A style locked before layer 2 can
actually run keeps the single gate **permanently**. Lock the styles that matter
only after `PROVIDER_MODE=live`.

## 4b. Wan 2.7's three modes, and the upload state machine

### Wan 2.7 is three DashScope models

The adapter already inferred a mode from the request shape — a reference video means r2v, a
first frame means i2v, text alone means t2v — and mapped each to its own DashScope model ID.
Two of the three IDs were undated placeholders. All three are now the reviewed runtime IDs:

| Mode | Model | Accepts |
| --- | --- | --- |
| T2V | `wan2.7-t2v-2026-06-12` | text, optionally images |
| I2V | `wan2.7-i2v-2026-04-25` | images, optionally text and audio |
| R2V | `wan2.7-r2v-2026-06-12` | image/video references plus text |

`.env` and `.env.example` carry all three, and `tests/test_provider_payload_contracts.py` pins
both the mapping and the mode inference, so a rename cannot happen quietly.

**The capability profile was the half that was actually missing.** `wan-2.7-official` declared
`supports_t2v` and nothing else, so `VideoModelRouter` and `CapabilityResolver` would never
select Wan for an image-to-video or reference-to-video shot: the adapter's i2v and r2v paths
were reachable only by naming the model explicitly. `wan-2.7-manual-v2` declares i2v, v2v,
start frame, reference images and audio. Registry defaults seed new databases only, so an
existing database keeps the old profile until it is updated through the registry API — that is
`docs/OPEN_ISSUES.md` §2.5, not a new problem.

**Wan 3.0 lost its built-in default.** `wan-3.0` mapped to `wan3.0-video`, an ID never verified
against DashScope. Wan 3.0 is invitation-only Beta, so a routed shot would have posted a guess
to a model the account cannot call. It now fails closed and names `WAN_VIDEO_MODEL_KEYS`, the
same rule Google Flow follows. The model definition and its FALLBACK binding stay registered as
capability records.

### The direct-upload chain had three defects, all in the same seam

The seam is that the quota hold is taken by the *route* while the state machine is owned by
`DirectUploadService`, and the two disagreed about what a failure means.

**A failed authorization kept the hold.** `reserve()` runs before `authorize()`, and nothing
released it when `authorize()` raised. Every failure after the reservation — a malformed digest,
an over-size declaration, a transient presign failure, and the `501` that local disk returns on
**every** attempt while `S3_*` is unset — permanently consumed workspace capacity. Worse, the
reservation stayed `RESERVED`, and `WorkspaceStorageQuota` answers a `RESERVED` key with "upload
is already in progress", so the client could never retry that key either. Reproduced end to end
before the fix: one `501`, 120 bytes held, `409` forever after. The route now releases in a
`finally`, and only a hold *that call* created — a replay must not release the hold belonging to
the authorization it is replaying.

**Re-authorizing was impossible for any workspace-backed project.** `DirectUploadService` has an
explicit replay path, and `test_an_authorization_replay_returns_the_same_upload` covers it — with
`workspace_id=None`. Through the route, `reserve()` was reached first and answered `409`. That
rule is right for a multipart upload, which really is in flight through this process, and wrong
for one the client holds its own presigned URL for: a lost response or a page reload is a replay
of one upload, not a second one. The route now finds the upload before reserving and reuses the
hold it already owns.

**Completing early destroyed a live session.** A missing object raised `DirectUploadNotFinished`,
and the completion handler released the hold and marked the row `ABANDONED` — while the presigned
PUT was still valid. A client that polled before its transfer finished lost the session, and the
bytes it then wrote had no row left to adopt them. A transient `read_prefix` failure did the
same. "Not there yet" is now distinguished from "cannot succeed": the row and its hold survive
while the window is open, and only a closed window is terminal. Re-authorizing a closed window
reclaims it too, which is the one place a stale hold is observable.

That last change is also what gives `expires_at` any meaning. It was written and indexed and no
query read it. It is still not swept — an upload authorized and walked away from keeps its row,
its object and its hold until the client returns — and `docs/OPEN_ISSUES.md` §2.11 has been
corrected, because it claimed the hold expired on its own. It does not.

Five regression tests in `tests/test_direct_upload.py` cover these; four of them fail against
the previous code.

## 4c. The Wan media plane, and single-owner completion

### Wan was throwing away references it had already paid to resolve

`_video_payload` read a reference video and a first frame. It never read
`reference_images` or `reference_urls` — the list the Gateway resolves, presigns
and bills for — so those inputs were dropped, silently, and the generation ran
without them. A shot built on four character plates rendered as if it had none.
Declaring `supports_reference_image` in the registry (previous section) would
have made that reachable by routing rather than only by hand.

Wan 2.7 carries every non-text input in one `media` array. Those were three
different instructions flattened into `img_url` and `video_url`:

| Role | What it means |
| --- | --- |
| `first_frame` / `last_frame` | the bracket frames of a shot |
| `first_clip` | footage the shot **continues from** |
| `reference_video` | footage the shot only takes motion or grade **from** |
| `reference_image` | a still that fixes identity |
| `reference_voice` | a voice the model conditions on — declared unsupported on 2.7 |

**The role is internal only.** On the wire each entry carries the two official
fields, `type` and `url`, and nothing else. The role still has to exist here —
it picks the model, enforces the per-role bounds and orders the array — but it
is dropped at the boundary. Which means **array position is the only role signal
the provider receives**, so the ordering is part of the contract, not tidiness:
the first frame leads, references follow.

The mode matrix, per the deployment documentation:

| Mode | Accepts | Framing sent |
| --- | --- | --- |
| T2V | `reference_image` | `resolution` + `ratio` |
| I2V | `first_frame`, `last_frame`, `first_clip`, `reference_image` | `resolution` |
| R2V | `first_frame` **+** `reference_image`/`reference_video` | `resolution`, `ratio` only without a first frame |

R2V taking a first frame *alongside* its references is the point of the mode,
and an earlier pass here had it backwards — that combination was rejected as
inexpressible. It is now what selects R2V.

Continuation belongs to I2V: a clip the shot grows out of is the same kind of
input as a frame it grows out of. So continuing from footage and *referencing*
footage select different models, and asking for both is refused rather than
silently resolved to one.

Bounds are the published ones, enforced before billing: one first frame, five
reference assets counting images and videos together. A drift gate asserts the
adapter's constants and accepted-role sets against the registry, because two
copies of one published limit is exactly how an over-long reference list ends up
refused in one place and routed in the other.

Two further rules keep the payload honest. Every media URL must be fetchable —
an unresolved asset ID reaching DashScope spends a generation on an input the
provider cannot read. And **a mode that cannot carry every supplied input is
rejected, not trimmed**.

### A capability flag is a promise the wire has to keep

`supports_audio` has always meant native audio **out** — the router reads it for
`requires_native_audio`. There was no flag at all for an audio asset carried
**in**, which meant a profile could imply voice conditioning that no adapter was
able to send. That is the same defect class as the reference images this adapter
used to discard, one level up.

`supports_reference_voice` is now its own capability (migration `0038`,
defaulting false everywhere). It is `false` for Wan 2.7, so no mode accepts a
`reference_voice` and a request carrying one fails closed before billing. The
serializer can nonetheless express one — `{"type": "audio", "url": …}`, still
only the two official fields — because the gate belongs in the declaration, not
in a whitelist that quietly cannot represent the thing.

`test_wan_declared_capabilities_are_ones_the_wire_can_actually_carry` binds the
two together in **both** directions: a flag set true with no mode accepting its
role is a promise the adapter would refuse, and a mode accepting a role no flag
claims is an input nobody authorised.

Framing was wrong too. `parameters.size` was receiving `"720p"` — a tier label
in a field that takes pixel dimensions — and `size` is not in the published
parameter set at all, so nothing sends it now. A caller asking for exact pixels
is refused rather than silently ignored.

The profile is `wan-2.7-manual-v3`, which separates continuation
(`supports_video_extension`, I2V), reference (`supports_v2v`,
`supports_reference_image`, `supports_multi_reference`,
`supports_character_reference`), voice (`supports_reference_voice`, false) and
edit (explicitly `supported: false`).

### Completion has one owner and one transaction

Two requests could both find the object and both adopt it. The `media_assets`
unique constraint resolved *that* — one was told `reused=True` — but that caller
settles **zero** bytes, so if it settled first the winner's settlement was
swallowed as a replay and the workspace never accounted for the object. The
upload row is now locked and exactly one caller leaves `PENDING`; the loser is
handed the winner's asset. Adopt, completion and settlement share one
transaction, so a process death between them rolls back to a `PENDING` upload
with its hold intact instead of leaving storage that is real and uncounted.

**That required removing a SAVEPOINT, and the reason is worth knowing.** Under
pysqlite, work inside `session.begin_nested()` survives a rollback of the
enclosing transaction. Verified directly: a plain insert rolls back, the same
insert inside a savepoint does not. So the "one transaction" would have been a
claim rather than a fact on the default database. `adopt_stored_object_in` now
reads before it inserts and uses no savepoint. The documented driver workaround
was tried and reverted — it makes every transaction take a write lock, and the
concurrency suites either fail on `database is locked` or hang. Seven other
call sites still rely on savepoints; that is now `docs/OPEN_ISSUES.md` §2.12.

### Three smaller ones in the same chain

- **A lost authorize race is a replay, not a 500.** The existence check and the
  insert are separate transactions. A workspace-backed project is serialized by
  the reservation's unique constraint; a project with no workspace had no guard,
  so the loser surfaced a raw `IntegrityError`.
- **`POST /internal/maintenance/expired-uploads`** reclaims uploads whose window
  closed and whose client never came back. Deliberately an endpoint rather than
  a loop: sweeping quota is an operational action on a schedule an operator
  owns. It does not delete the orphaned object, and it *reports* stale holds
  rather than releasing them — a hold whose registration succeeded and whose
  settlement failed must survive, and only an operator can tell the two apart.
- **One lineage key.** `media_service.lineage_key` is the single definition, and
  completion consumes the value the authorization already stored on the upload
  row. Deriving it twice made deduplication depend on two formulas agreeing.

## 5. Also closed on 2026-08-23

| Was | Now |
| --- | --- |
| Style lock reached the prompt only via `prepare_autopilot`; every other caller of `compile()` silently produced prompts with no style lock | `PromptCompilerService` resolves the project lock from `ProjectStyleService` itself. A caller-supplied `style_lock` cannot override the authoritative one. `scripts/simulate_short_story.py` dropped its mirror of the old injection and is now the proof. |
| `MemoryQuery` had no `episode_id` and no recency weighting | `episode_id`, `EpisodeScope` (`EPISODE`/`SERIES`) and `recency_half_life_days`. Scoping is per layer now: L0 series-wide, L1 scene-fenced, L2 episode- or series-scoped. **L2 was previously fenced to the current scene**, so the layer whose purpose is recalling earlier work could not see any of it — that is why the 60-episode case never worked. |
| `VIDEO_GROK` / `VIDEO_VEO` kept FALLBACK bindings on transportless stubs | Both bindings removed; the model definitions stay as capability records. The integrity gate now rejects a binding of *any* kind onto a stub, not just a PRIMARY. |
| `RUNAPI_BASE_URL` / `RUNAPI_MODEL_ID` empty | Configured. `RUNAPI_IMAGE_PATH` and `RUNAPI_VIDEO_PATH` were missing from `.env` and are now present. |
| `references/` had no Wan 3.0 envelope and no Seedance 2.5 entry | Both added, plus a new `gpt-image.md`. Omni still blocked on the transport decision. |

## 6. What changed in the 2026-08-22 session

### Provider / payload boundary (was P1, now closed)

| Defect | Fix |
| --- | --- |
| Retry reused the previous attempt's `provider_payload` | Recompiled for the actual target; dropped when it cannot be recompiled |
| Plan admission re-routed the model but kept the old payload | Payload cleared on re-route |
| URL-mode providers were sent unusable local/provider media IDs | `GenerationProvider.reference_mode`: `PROVIDER_MEDIA_ID` vs `FETCHABLE_URL`, fail closed |
| Flow silently degraded unmapped models to `abra_t2v_{duration}s` | Reviewed mapping + `FLOW_VIDEO_MODEL_KEYS`, fail closed |
| Flow image silently defaulted to `NARWHAL` | Reviewed image-model set, fail closed |
| Wan: registry logical name posted to DashScope as a model ID | Mode+version aware resolution via `WAN_VIDEO_MODEL_KEYS` |
| OpenRouter / RunAPI / Ark forwarded internal metadata to providers | Explicit field allowlists |

The last one leaked `project_id`, `shot_id`, `cost_estimate`, `generation_policy`,
`style_control` (embedding vector) and the whole `metadata` blob including
`canonical_shot_spec` and router decisions. It no longer does.

### Skills

All twelve `SKILL.md` bodies rewritten. **Authorship note:** the earlier handoff recorded
these as user-authored; on 2026-08-22 the user explicitly directed the agent to write all
twelve. If that reverts, record it here — do not assume either rule.

`prompt-compiler` satisfies the eight-field `PromptCompilerOutput` contract, the
`PromptCompilerInput` envelope, and real `CanonicalShotSpec` field names.
Verify with `uv run python scripts/review_skill_contract.py skills/*/SKILL.md`.

### Model registry

20 models, 22 roles, four credentials at the time (21 and 23 after the 2026-08-23 image
model and `IMAGE_GENERATION` role). `gpt-5.6-sol` went from 7 primary text roles to 1
(`CINEMATOGRAPHY_REASONING`). Added: Claude Opus 5 (DIRECTOR), Qwen3.8-max (prompt roles,
via the DashScope compatible-mode transport the Wan provider already had), GLM-5.2 (Ark
third-party), DeepSeek-V4-Flash, Grok Imagine Video (OpenRouter), Wan 3.0, Seedream 5.0.
New roles: `CAMERA_MOVEMENT`, `CAMERA_OPERATOR`, `USER_QA`.

### Narrative ledger (migration `0034`)

`narrative_facts`, `narrative_disclosures`, `narrative_obligations` +
`core/narrative-ledger/narrative_ledger_core`. Answers what `TimelineState` and
`CharacterStateVersion` cannot: **who may know what**, and **what the series still owes**.

- A fact defaults to audience-only knowledge; a character must be disclosed to separately.
- `assert_may_act_on` fails closed — audience knowledge never authorises a character.
- Obligations are *owed*, not *similar*, so embedding retrieval can never surface them.
- `series_context()` is O(1) in episode count: heads, not history. This is what makes 60
  episodes tractable.

## 7. Files worth knowing

| Path | Purpose |
| --- | --- |
| `core/narrative-ledger/` | The series ledger service |
| `skills/model-prompting/references/gpt-image.md` | How to phrase a still for the project's image model |
| `services/media-service/media_service/renditions.py` | Original vs derived encodings; the rule that the original is never touched |
| `core/style/style_core/semantic.py` | The layer-2 boundary; owns no model choice |
| `services/media-service/media_service/direct_upload.py` | Authorize/adopt for uploads that never enter this process |
| `tests/test_direct_upload.py` | The store counts the API's reads: one HEAD, one bounded range, never the body |
| `tests/test_media_reference_plane.py` | Originals survive, derivations are bounded, references never proxy |
| `scripts/review_skill_contract.py` | Reviews a Skill against the contract; installs nothing |
| `scripts/simulate_short_story.py` | Offline 3-shot end-to-end run; now also the style-lock proof |
| `tests/test_openrouter_image_generation.py` | The image path: allowlist, envelope, editing, completion, route |
| `tests/live/test_openrouter_image_live.py` | Opt-in live image verification; one free test, one billed |
| `tests/test_model_routing_integrity.py` | Drift gate: no unreachable/ambiguous/placeholder routes |
| `tests/test_provider_payload_contracts.py` | The payload/reference defects, plus the video job lifecycle |
| `tests/test_installed_skills.py` | All twelve Skills parse and stay resolvable |
| `tests/test_narrative_ledger.py` | Dramatic irony, obligations, constant-cost context |

## 8. Open items

**The complete list, with your action items first, is in
[`docs/OPEN_ISSUES.md`](docs/OPEN_ISSUES.md).** Summary below.

**Blocked on the user**

- **`PROVIDER_MODE=live` is the only live gate still unset.** `ALLOW_LIVE_PROVIDER_CALLS` and
  `LIVE_PROVIDER_CONFIRMATION` are already correct. The agent must not flip the last one: it is
  the switch that makes every provider transport billable.
- **Object storage is not configured.** `S3_*` are empty, so the storage backend cannot presign.
  Two things depend on it: every reference-carrying shot (including every image *edit*) fails
  closed, and `POST /v1/assets/uploads` answers `501`. The signed local route is enabled for
  development only; it proxies through the API and must not be the production answer. Configure
  S3/R2/MinIO and an HTTPS `PUBLIC_BASE_URL`.
- **Lock styles after `PROVIDER_MODE=live`, not before.** Layer 2 is enabled but cannot run in
  mock mode, and `ProjectStyleLock` is append-only — a style locked now keeps the single gate
  permanently.
- `FLOW_VIDEO_MODEL_KEYS` is empty, so `flow-veo-3.1` returns `FLOW_MODEL_KEY_NOT_MAPPED`.
- Omni Flash is **Google Gemini Omni Flash**, not ByteDance. Needs a transport decision
  (Gemini key, or the user's own Flow reverse proxy).
- Keys pasted into chat (Ark, DashScope, RunAPI) should be rotated.
- Chinese→English conversion was requested but **not done**: most Chinese in the codebase is
  functional NLP logic (`edge.py` numeral/negation regexes, `corrector.py` 82 keyword lines,
  `narrative_core/compiler.py`, camera-gaze patterns) plus the entire Web UI. Translating it
  deletes Chinese-language capability from a `zh-CN` product. Needs an explicit scope decision.

**Known defects, not yet fixed**

- Retrieval is keyed on the current shot's prompt text, so narrative dependency is invisible
  to it. The ledger covers obligations and retrieval now reaches across episodes, but *which*
  earlier beat matters is still decided by similarity alone.
- A synchronous provider's result is held in the Gateway process between confirmation and
  poll. Losing it reconciles rather than refunds, but making it durable needs a migration.
- `MediaRenditionKind.THUMBNAIL` exists in the schema and nothing generates one yet; the UI
  still reads originals.
- Derived renditions are never garbage-collected. They are content-addressed and small, but a
  provider that changes limits repeatedly accumulates copies with no retirement policy.
- `configure_runtime_model` only reconciles models created by this startup's default sync, so
  adding a credential later does not re-enable a model that was disabled for want of one.
- Aggregate style drift across episodes is unmonitored (per-candidate only).
- `timeline_scope_key` branch proliferation has no retirement policy.

## 9. Git state

**Committed at `ea9d042`** on `main`, on 2026-08-23. Everything described in this document is
in version control; the working tree is clean.

That commit carries eight workflows at once. They were never split, and by the time they were
committed they had developed on top of each other in `container.py`, `models.py`, `main.py`,
`gateway.py` and the media/style services, so no hunk-level separation was possible after the
fact. The tip passes every gate. The per-workflow history that would have shown each change
alone does not exist and cannot be reconstructed:

1. Persistent Character State / Project Style Lock follow-ups
2. Alchemy / Wallet / DePay / Web wallet and migrations `0030`–`0033`
3. Adapter payload → Provider handover fixes
4. Unified Prompt/Skill base and the final input/output contract
5. 2026-08-22: model registry, skills, narrative ledger, migration `0034`
6. 2026-08-23: the `openai/gpt-image-2` image path, `IMAGE_GENERATION` role, style-lock
   enforcement in the compiler, episode-scoped retrieval
7. 2026-08-23: the media plane (presigned references, `media_renditions`, migration `0035`),
   batch candidates, and the semantic style layer (migration `0036`)
8. 2026-08-23: direct-to-storage uploads (migration `0037`) and enabling layer 2

**Going forward, commit per workflow.** The reason this one is a blob is that five sessions
went by without committing; the cost is that `git log` cannot answer "when did the media plane
change" for anything before `ea9d042`.

That commit also fixed a `.gitignore` defect worth knowing about: `references/` was unanchored,
so it matched `skills/*/references/` as well as the root directory. Both Skill reference
libraries — the ones `model-prompting` and `image-prompt-corrector` instruct an agent to read —
had never been in version control. A fresh clone would have had Skills pointing at files that
were not there. The patterns are anchored now (`/references/`, `/data/`, `/output/`, `/dist/`).

The repository has **no remote**, so nothing is pushed and "pushed" cannot be verified for any
commit. Nothing outside this machine has a copy.

## 10. Model-backed prompt compilation (not started)

`compile_input()` is deterministic. `skill_contract()` exposes the system prompt and both
JSON Schemas but never calls a model. The `prompt-compiler` Skill now satisfies the
contract, so the technical precondition is met. Remaining steps:

1. Call `ModelRoleRuntime` through `ModelRole.PROMPT_COMPILER`.
2. Accept JSON only; parse into `PromptCompilerOutput` first.
3. Re-verify input facts, asset echo, continuity echo, forbidden Provider fields, and that
   no new facts were introduced.
4. Fail closed on model or validation failure. **Whether a deterministic fallback is
   permitted is an open product decision** and must be recorded in the audit trail — it is
   not an implementation detail to settle in passing.
5. Write a `ModelExecutionRecord` carrying no secret and no unnecessary raw response body.

## 11. P2 — data and release blockers

- Migration head is `0034_narrative_ledger` with offline/temporary-database evidence only.
  Production-shaped populated upgrade and rollback are unverified.
- No live evidence for Payment, Provider, VLM, real billing or real on-chain payment.
- Email verification, MFA, member/device sessions, production secrets/HTTPS, backup and
  restore, monitoring and alerting, and operational process all still block release.
- Production visual detector / tracker / encoder and a trusted `VLM_REVIEWER` are neither
  deployed nor calibrated.

## 12. Standing rules

- Providers are offline by default; live needs all three gates plus a `LiveCanaryPermit`.
- Adapter payloads may never carry tenancy, accounting or audit fields.
- A logical model name must never reach a provider as an API model ID.
- Voyage is `ADVISORY` only — never identity, state, delta or commit authority.
- The agent does not write credential values into files.
