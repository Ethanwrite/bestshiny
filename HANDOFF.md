# AI Director Platform — Handoff

Date: 2026-08-25 · Branch `main` · commit `ea9d042` · **NOT PRODUCTION-READY** · no remote

This is the single current entry point. It supersedes the 2026-08-20 and 2026-08-22
development handoffs and the Visual Runtime implementation record, all three deleted
because they described states the code no longer has.
Architecture truth lives in [`CURRENT_ARCHITECTURE.md`](CURRENT_ARCHITECTURE.md).

## 1. Gate state (all green, offline only)

```
.venv/bin/python -m pytest -q                      709 passed, 9 skipped, 63 warnings   (SQLite)
POSTGRES_PASSWORD=... \
  .venv/bin/python -m pytest -q --database=postgres  711 passed, 7 skipped, 63 warnings   (PostgreSQL)
.venv/bin/ruff check .                             All checks passed
.venv/bin/python -m mypy                           Success: 137 source files
.venv/bin/python -m alembic heads                  0041_embedding_space (single head)
```

**Both halves must be green.** The PostgreSQL half needs `docker compose up -d postgres`. The SQLite
half skips tests marked `postgres_only` — behaviour that only exists where transactions genuinely run
concurrently, which is not a coverage gap but the absence of the phenomenon.
`POSTGRES_KNOWN_DIVERGENCES` in `tests/conftest.py` is currently empty; entries there are strict xfails,
so fixing one fails the run until its entry is removed. Local `DATABASE_URL` is now the compose
PostgreSQL and startup no longer creates tables — run `alembic upgrade head` first.

The skipped are the opt-in live tests (7 of them; SQLite skips two more that are
marked `postgres_only`). They need `--run-live-provider` *and* the three-part gate —
and the two new Wan I2V/R2V ones additionally need object storage, without which
they skip rather than inventing a reference URL.

**Wan 2.7 T2V is verified live** (2026-08-25). `PROVIDER_MODE=live` on the user's
instruction; task `285f787d-c1fe-40c5-8893-6e1f89adbb70` submitted, polled and
`COMPLETED` with a fetchable `video/mp4` artefact — auth, the DashScope async
protocol and the poll parsing all confirmed against the real service. Known spend
is **no longer USD 0**: one 5s 720P clip.

**Wan 2.7 I2V and R2V are now verified live too** (2026-08-25), against the
corrected protocol in §12d:

```text
i2v  task fb7cf016-479d-4816-a066-8894525466d8   COMPLETED   413,652 B  ftyp/isom
r2v  task 57ba09a0-a0e2-4c16-8914-daabebfb836b   COMPLETED   412,120 B  ftyp/isom
```

Two 2-second 720P clips. Both artefacts were range-fetched and are real MP4
containers, not just URLs. What those two calls establish, that the T2V canary
could not: `input.media` is accepted with `type` carrying the **role**
(`first_frame`, `reference_image`); `input.negative_prompt` is accepted where it
now lives; `duration: 2` is accepted, confirming the corrected floor; R2V accepts
`parameters.ratio` when no first frame fixes the aspect; and a request carrying
no `parameters.audio` is accepted. The reference plate was fetched by Alibaba
from a presigned OSS GET — the whole `FETCHABLE_URL` path working end to end for
the first time.

Known spend: three clips — one 5s T2V, one 2s I2V, one 2s R2V.

**One thing these runs do not establish.** They prove the corrected body is
*accepted*. They do not prove the previous body would have been *rejected* —
that comes from the published API references, which define these strings as the
valid `media.type` values. Confirming it directly means posting the old
`{"type": "image"}` form and seeing a 400; a rejected request is free, but if it
were accepted instead it would bill another clip. Not run.

The earlier T2V-only result should still be read narrowly, for the reason it
always should have been: T2V is the one mode with no `media` array, and that
canary set no negative prompt and no audio, so it exercised none of the fields
§12d found wrong.

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

## 2. 2026-08-25 — one schema authority, and PostgreSQL as the only runtime

`Database.create_all()` ran from `build_container()` on every startup. It creates tables missing from ORM
metadata, never adds a column to a table that already exists, and never advances the alembic stamp — two
authorities over one schema. That is gone. Startup now compares the stamped revision to
`REQUIRED_SCHEMA_REVISION` and refuses to run otherwise, naming `alembic upgrade head` when it does.
`create_all_and_stamp()` survives for throwaway databases and runs only under `DEPLOYMENT_ENVIRONMENT=test`.

Production also refuses a non-PostgreSQL `DATABASE_URL`, for the reason in §2.12: under pysqlite a
`begin_nested()` savepoint does not roll back with its enclosing transaction, and seven call sites depend on
that rollback. Local `.env` now points at the compose PostgreSQL, published on `127.0.0.1:5432`.

The credential-vault check moved above the first connection, so every production guard is now a configuration
guard evaluated before any I/O. A misconfigured deployment is refused for the reason it is misconfigured.

### The migration evidence §11 said was missing

Run against the compose PostgreSQL, which held a real (if small) populated schema — 13 `model_definitions`
rows, stamped eleven revisions back at `0027`:

```
0027 -> 0038   populated upgrade                    all 11 revisions applied
0038 -> 0034   downgrade across 0035-0038           applied
0034 -> 0038   re-upgrade                           applied, 13 rows intact
0038 -> 0039   new revision, up and down            applied both ways
```

That is not a production-shaped dataset, and it is not a restore drill. It is the first populated
upgrade/rollback this project has on PostgreSQL rather than on an empty temporary database.

### The test matrix

```bash
.venv/bin/python -m pytest -q                                          # SQLite
POSTGRES_PASSWORD=... .venv/bin/python -m pytest -q --database=postgres  # PostgreSQL
```

The PostgreSQL half reroutes the shared `container` fixture into a throwaway schema inside a dedicated
`video_platform_test` database — dedicated because a test schema whose `search_path` can also see a populated
`public` is not isolated: `create_all(checkfirst=True)` finds each table already there, skips it, and every
test then shares one copy. About fifteen test modules build their own `Settings` with a hard-coded SQLite URL
and are **not** rerouted.

### Three defects it found on the first run, and one it could not fix

- **`create_all` could not build the schema on PostgreSQL at all.** `enforce_payment_ledger_append_only()` is
  declared through SQLAlchemy's `DDL`, which percent-interpolates its statement, and carried a single `%` in
  `RAISE EXCEPTION '% is append-only'`. `TypeError` before the trigger could be created. Latent because
  `create_all` had never been run against PostgreSQL; the migration uses `op.execute`, which does not
  interpolate, so production was never affected.
- **Eight integrity guards raised the wrong SQLSTATE.** The asset-registry and project-style plpgsql guards
  declared no ERRCODE, so PostgreSQL reported `P0001` → `ProgrammingError`, while the identical SQLite guards
  raise `IntegrityError`. `except IntegrityError` caught on the development engine and not on the production
  one. Migration `0039_integrity_errcodes` replaces both functions with `USING ERRCODE = '23514'`; a test now
  fails if any guard omits its SQLSTATE. The character-state head fence keeps `40001` on purpose — a stale
  fence means retry, not invalid data — and its test accepts either class with the same message.
- **A duplicate generate request 409d instead of replaying, under real concurrency.** Fixed; see §4. The test
  that caught it was written to force this race and had passed on SQLite for years only because SQLite
  serialises the two transactions — a false green that stood until the matrix existed.

## 3. 2026-08-25 — a paid artefact that lived in one process

`docs/OPEN_ISSUES.md` §2.4, closed. A synchronous image API answers with the artefact in the response body:
no remote job to re-read, no URL to fetch. Once the submission was confirmed, the bytes existed in exactly
one place — a dictionary on the Gateway object — until the poll consumed them, both inside one `process()`
call. A worker dying in that window lost a result the workspace had already been billed for. Never a silent
success or refund (`get_job` reported the provider's not-retrievable error with `submitted=True`, and the
credit moved to `RECONCILIATION_REQUIRED`), but still a paid artefact that no longer existed.

Migration `0040_sync_result_inbox` adds `provider_synchronous_results` and its ordered
`provider_synchronous_result_outputs`. Three things make it an answer rather than a relocation of the
problem:

- **Written in the transaction that confirms the submission.** The result becomes durable at exactly the
  moment the workspace becomes liable for the call. There is no ordering in which one holds and the other
  does not.
- **Deleted in the transaction that marks the job terminal — not on read.** The first version of this
  consumed the row when the poll read it, which just moved the fatal window to "between the read and the
  completion commit". A poll that reads the bytes and then dies now finds them again. A test asserts two
  consecutive reads return the same bytes.
- **Digest-checked on read.** Each output stores a SHA-256 verified when it is read back. A mismatch raises
  with `submitted=True`, so corrupt bytes reach reconciliation instead of being published as a paid result.

A stale row from an earlier attempt names a different `provider_job_id`; it is discarded, never returned —
a result for a submission this poll did not make is not one this poll may complete. `FAILED` discards its
row too, since nothing will consume it; `RETRY_WAIT` and `WORKER_NEEDS_USER_ACTION` keep theirs, because
those jobs still have an attempt ahead of them.

The test that proves it drives the completion from a **different** `GenerationGateway` object, constructed
over the same database and sharing nothing else with the one that took the submission.

`content` is `bytea` rather than an object-storage key because this is an inbox, not the media plane. The
bytes move into the media plane the moment the poll completes, through the same content validation a
downloaded artefact passes, and the row is transient by construction — one per in-flight synchronous job,
removed on completion, cascaded with the job.

## 4. 2026-08-25 — a fence that failed the request it was protecting

`docs/OPEN_ISSUES.md` §2.20, closed. Two requests carrying the same `idempotency_key` reach the Gateway
together. Both read the idempotency table and find nothing. Both enter the create transaction, and there
they serialise — on `SELECT ... FROM shots ... FOR UPDATE`, which the timeline fence needs.

The winner validates its fence, creates the job, claims the key, and moves the Shot to `QUEUED`. The loser's
`FOR UPDATE` returns the moment the winner commits, and returns the Shot the winner just changed. Its fence
is now stale — against a change its own duplicate caused — and it raised
`shot or authoritative timeline binding changed; plan the shot again`. A 409, telling the caller to re-plan
work that was already running.

The replay path for exactly this race was three statements further down: `session.add(GenerationIdempotency)`
would have hit the unique constraint, and `except IntegrityError` would have found the winner's record and
replayed it. The fence sits in front of it, so that path was unreachable once a competitor had committed.

A stale fence is now conclusive only once the key is known to be unclaimed. If a claim exists it is a claim
for this same request — `replay` still refuses a key whose request hash differs — and the idempotent answer
is the competitor's job.

Ordering the claim ahead of the fence would arrive at the same place through `IntegrityError`, and was the
first thing considered. It was not taken: the idempotency row carries a NOT NULL foreign key to the job, so
claiming first means writing a job row before the plan behind it has been validated. Reading the fence
failure correctly costs one query and moves nothing.

### Why this survived until now

The test that catches it, `test_concurrent_generate_requests_replay_one_candidate_and_job`, was written to
force this exact race — it puts a barrier on the idempotency lookup so both requests are guaranteed past it.
It passed for as long as it existed, because SQLite serialises the two transactions outright and the loser's
opening lookup already finds the winner's claim. Instrumented, the new branch is taken **once** on PostgreSQL
and **zero** times on SQLite.

Two regression tests, both verified to fail when the handler is removed:

- the threaded one, which now covers the branch deterministically on PostgreSQL because the loser cannot
  read the Shot until the winner commits;
- `test_a_duplicate_request_replays_when_the_competitor_moved_the_shot`, marked `postgres_only`, which opens
  the window directly rather than relying on scheduling.

`test_a_stale_fence_is_still_a_conflict_when_no_competitor_claimed_the_key` runs on both engines and guards
the other direction: with no claim there is no competitor, and the 409 stands.

One thing the loser still leaves behind: its own `prompt_compilations` row, written before the create
transaction. It is a shot-scoped, append-only record that a prompt was compiled, which the loser did do;
nothing references it as authoritative state and no job or candidate is duplicated.

## 5. 2026-08-25 — a lock that could quietly become permanent

`docs/OPEN_ISSUES.md` §2.18, closed. With `FEATURE_SEMANTIC_STYLE_LOCK=true`, a style lock made while
`google/gemini-embedding-2` was unreachable committed anyway: `style_layers: 1`, a
`semantic_layer_absent_reason`, done. `ProjectStyleLock` is append-only and a database trigger forbids
re-locking, so that single gate was the last word on how every candidate in the project would ever be judged
— and the row was indistinguishable from one made deliberately with the feature off.

A transient outage at the embedding provider therefore silently and permanently downgraded a project. The
project's own documentation had a standing warning about it (§1.10, "lock your styles *after* going live"),
which is the shape of a procedure standing in for an enforcement.

`lock()` now refuses. Nothing is written, the project stays lockable, and a retry once the model answers
produces the two-layer lock that was asked for in the first place. Refusing costs a retry; degrading cost the
second gate for the life of the project.

The two refusal causes do not deserve the same answer, so they do not get one:

| Cause | Status | Why |
| --- | --- | --- |
| `SEMANTIC_MODEL_UNAVAILABLE:…` | `503` | The model can come back. Waiting is worth something. |
| `SEMANTIC_REFERENCE_MEDIA_UNREADABLE` | `409` | The media will not become readable. Telling the user to retry would be a lie; they need a different style version. |

With the feature **off** there is no embedder at all, a single-layer lock remains the intended outcome, and it
still records `SEMANTIC_EMBEDDER_NOT_CONFIGURED` so it cannot be mistaken for an accident. Fail-closed applies
to the layer being enabled, not to its absence.

`scripts/preflight_live.py` gained a **Project style lock** section, because "will locking work" became a
question worth answering before trying rather than after. It currently reports `AT RISK`: the gate is open and
the credential is set, but that model has still never been called, so the first lock is also its first live
exercise.

Four tests, all verified to fail with the guard removed: the refusal and its successful retry, the
non-retryable cause, both route status codes, and the feature-off case that must still lock.

### Two mirrored type bugs found while reading it

`ensure_embedding()` and `semantic_reference()` each have two `existing` checks around their embedding call —
one before, one after, for the race where another transaction creates the same row meanwhile. The second check
in each had the *other* function's return type: `semantic_reference` returned a bare `StyleEmbedding` where a
`SemanticReferenceAttempt` was expected, and `ensure_embedding` returned a `SemanticReferenceAttempt` where a
`StyleEmbedding` was expected. A copy-paste swap, reachable only on the race path, where `lock()` would then
read `.embedding` or `.id` off the wrong object. Both fixed; mypy caught neither, because `session.scalar()`
is typed loosely enough to pass.

## 6. 2026-08-25 — a number that meant nothing and looked fine

`docs/OPEN_ISSUES.md` §2.19, closed. A similarity score is only meaningful inside one vector space. Change the
model, its revision, the dimension count, how the stored vector is normalized, or the metric — and the same
number means something else.

The failure mode is that nothing fails. Cosine over two vectors from unrelated spaces does not raise; it
returns a perfectly plausible 0.83, and the style gate goes on issuing confident PASS and FAIL verdicts about
a comparison that stopped meaning anything. `StyleEmbedding` already recorded `provider`, `model`,
`algorithm_version` and `dimension` — and **only `model` was ever read**, and only to find a row, never to
decide whether two vectors could be compared at all.

`EmbeddingSpaceIdentity` (`core/style/style_core/space.py`) is the seven fields that must match:

```
provider   model   model_revision   input_schema_version   dimension   normalization   distance_metric
```

Migration `0041_embedding_space` adds the three that were missing. The backfill is exact rather than assumed:
every existing row was written by this codebase, whose `aggregate()` L2-normalizes and whose `similarity()` is
cosine, so `L2`/`cosine` are the values those rows actually have. `model_revision` backfills empty because no
provider wired here publishes one. The server defaults are dropped once the backfill is done, so a later
insert cannot omit a field and silently acquire a space it never verified.

Compared at three points, all of which refuse rather than degrade:

| Where | On mismatch |
| --- | --- |
| Reusing a stored reference to lock | Lock refused (`409`, non-retryable — waiting cannot change which space the embedder produces) |
| Scoring layer 1 | `REVIEW_REQUIRED`, `STYLE_EMBEDDING_SPACE_CHANGED:<fields>`, no score |
| Scoring layer 2 | `REVIEW_REQUIRED`, `STYLE_SEMANTIC_EMBEDDING_SPACE_CHANGED:<fields>`, no score |

No score, not a low one. A meaningless comparison does not have a value; giving it one would let a threshold
decide something it cannot know. The reason code names the fields that moved, so the answer to "why is every
candidate in review" is in the evidence rather than in a bisect.

Layer 2's space is read **after** the call, never before: which model answers is the role runtime's decision,
made per call, and a fallback binding can move the space between one candidate and the next.

### What this still cannot see

`model_revision` is only what a provider echoes back, and none of the models wired here publishes one. A
silent provider-side swap behind a stable model id, whose output dimension is unchanged, remains undetectable
locally — recorded as the residual on §2.19. Everything detectable without the provider's cooperation is now
detected.

`capability_profile_version` was in the original design and is deliberately not stored: its only failure mode
independent of the fields above is a changed declared dimension, and `dimension` already catches that.
Recording it would have been a field that cannot move on its own.

Narrative memory turned out **not** to have the same hole — retrieval already filters candidates by provider,
model and dimension before scoring. It filters on less than the style gate now compares, which is recorded as
§2.23.

## 7. 2026-08-25 — a plan that decided whether you paid at all

`docs/OPEN_ISSUES.md` §2.17, closed. `reserve_generation` opened with this:

```python
if balance.workspace_id is None or balance.plan_tier != "FREE":
    return WorkspaceCreditCharge(False, False, None, 0, balance.balance, None)
```

So a PRO or ENTERPRISE generation was priced by the server, the quote was written onto the job, and then
nothing happened to it. No reservation, no ledger entry, no settlement — and, worse than the missing revenue,
no credit held when a provider result came back ambiguous. The reconciliation machinery that exists precisely
for "we were billed and cannot tell what we got" had nothing to hold for any paying customer.

Who pays is now one property on the balance, read by both the service and the Gateway:

```python
@property
def billable(self) -> bool:
    return self.workspace_id is not None and self.plan_tier != "ALL"
```

Every plan pays. A plan sets the grant, the discount and which models may be used — FREE is still routed to
Seedance and nothing about that changed — but not whether a generation costs anything.

### The one exclusion, and why it is not a plan

`plan_tier="ALL"` is the workspace the API creates when authentication is disabled. Its own comment says it:
*"the explicit legacy bypass surface; it still receives server pricing/CostRecords but must not consume a
real Free-plan wallet."* My first cut charged it, and five tests failed with `402 Payment Required` — local
development spending a real balance. `ALL` is the absence of a plan, not a tier, and it is excluded by the
same property that includes every real one.

### 402, not 403

`InsufficientWorkspaceCredits` and `PlanEntitlementDenied` shared a 403. That cost nothing while a paid tier
could not run out of credits. Now that it can, the two are different problems with different fixes — top up
versus upgrade — and a client that cannot tell them apart cannot route the user to the one that helps. Five
call sites split.

### What this makes visible

A new workspace holds 50 credits. Seedance 2.5 — the model a FREE workspace is routed to — quotes 44 credits
at the 4-second Passenger default, 54 at 5s, 87 at 8s. Flow Veo 3.1 at 8s quotes 192. So the grant buys one
short clip and nothing else, and anything longer than the default is refused before a Job exists.

That was already true and is not a consequence of this change — FREE was always charged. It is recorded as
§2.24 because the change makes it reachable for every tier that has not topped up, and because the paid tiers
have no grant of their own beyond the same 50: only a DePay purchase adds 3,000. Whether that is right is a
pricing decision, not an engineering one.

### Correcting what I said earlier

I told you this had to ship alongside a top-up path. It did not: the DePay checkout already credits any
workspace in {FREE, PRO} and `GET /api/workspaces/{id}/wallet` already reports balance, reserved and
purchased credits. A paid workspace that runs out is not stuck. What is genuinely missing is the *recurring*
grant — there is no billing period on `Workspace`, no renewal and no grant primitive — and the plan
*discount*, which `CreditPricingEngine` has a multiplier for that no tier feeds. Both recorded under §1.12.

## 8. 2026-08-25 — the External Evidence Registry, and what it found

Built from two research documents supplied by the user, compiled into
`config/external-evidence/registry-v1.json` with a loader, a schema and 22 tests. The point of it is
not the numbers; it is that the numbers cannot be moved onto the wrong model.

### The finding that shaped it

Both documents state a version lock — `Seedance 2.0 ≠ 2.5`, `Wan 2.1 ≠ 2.7 ≠ 3.0`, `Veo 3.1 ≠ 3.1 Fast`,
`Kling 2.5 ≠ 3.0`. Applying it to the models this platform actually runs:

| Model | Prior-eligible metrics | Capabilities with a prior |
| --- | ---: | --- |
| `veo-3.1-fast-openrouter` | 10 | prompt_adherence, temporal_consistency, identity_consistency, visual_quality |
| `gpt-image-2-openrouter` | 12 | prompt_adherence, physical_realism, reference_adherence, visual_quality |
| `wan-2.7-official` | 1 | visual_quality |
| `kling-3-pro-openrouter` | 1 | visual_quality |
| `kling-3-standard-openrouter` | 1 | visual_quality |
| `veo-3.1-openrouter`, `flow-veo-3.1-internal`, `veo-3.1-lite-openrouter` | 1 each | visual_quality |
| `seedance-2.5-official` | **0** | — |
| `seedream-5.0-ark`, `grok-imagine-video-openrouter`, `grok-video-official`, `flow-narwhal-image-internal`, `veo-3.1-quality-official` | **0** | no public evidence at all |

Every rich diagnostic number in the research — Wan 2.1's Physical Plausibility `.939` and Camera Control
`.527`, Kling 2.5 Turbo's OSCBench, Seedance 2.0's Chinese multi-speaker dialogue scores — belongs to a
version this platform does not run. `seedance-2.5-official` has **31 metrics on file and zero eligible**.

The one exception arrived mid-session: the user supplied OpenRouter ids for Veo, which makes
`google/veo-3.1-fast` runnable — and OSCBench evaluated that exact variant. It is now the only video model
here with per-dimension external evidence.

So the registry's immediate value is not a better prior. It is that every `capability_prior` in
`config/model-registry/defaults.json` is a hand-authored `MANUAL_PRIOR`, and the registry says which of
them have public backing and which are judgement. `GET /internal/models/external-evidence` reports the
exclusions, not just the priors, because the operator's real question is *why is this still a hand-written
number*.

### What the registry refuses to do

The near-miss evidence is recorded rather than deleted, and bound to the model it would tempt someone to
attach it to, marked `VERSION_MISMATCH`. Nine of those are asserted by name in the tests, each one a number
a reasonable person would otherwise reuse. Deleting them would only mean the next person re-derives them
from the same public source and this time attaches them silently.

Also enforced: a record's grade is the **weakest** source it cites, so one A source does not launder a C
source beside it. Aggregates (`Wan-Bench Weighted Score .724`, `Qwen-Image-Bench Overall 64.69`) are stored
with `mapping_confidence: LOW` and can never stand in for a capability. Human and automatic judge scores on
the same OSCBench dimension are stored separately and never averaged — on Wan 2.2's OSC Consistency the
automatic judge is *higher* than the humans. A source that published words instead of numbers (Veo 3's model
card: "best", "preferred") stores `value: null`.

### Configuration changes shipped with it

- **Three OpenRouter Veo models added**: `google/veo-3.1` (PREMIUM, all criticalities),
  `google/veo-3.1-fast` (STANDARD, not canonical), `google/veo-3.1-lite` (DRAFT, edge/temporary only).
  Envelope per the operator: 4/6/8s, 720p/1080p, text and image input, first/last frame, and synced audio
  on **all three** variants. Audio was initially declared on the main variant only, on the project's
  under-declare-rather-than-assume rule; the operator confirmed Fast and Lite carry it too.
- **`wan-3.0-official` disabled.** This account has no Wan 3.0 API access. Disabled in the registry rather
  than left to the runtime's missing-reviewed-id gate, so it never enters a candidate list at all.
- **Startup does not replay defaults over an existing row**, by design, so `enabled: false` in the config
  did not reach the already-seeded development database. `wan-3.0-official` was disabled there through
  `configure_runtime_model`, which is the sanctioned operator path. A fresh database picks it up from the
  config. Worth remembering for any future capability edit: changing `defaults.json` alone moves nothing on
  a database that already has the model.
- **Consequence, asserted by a test:** Wan 3.0 was the only 30s model. Disabling it puts the duration
  ceiling back at 15s, so a 20s shot is unroutable again and fails with `DURATION_UNSUPPORTED` before a Job
  exists.
- `FEATURE_EXTERNAL_PRIOR` added, **default false**. The registry ships as a read-only data asset first;
  publishing it changes nothing about which model gets picked.

### Not built

The router does not consume the registry yet. Confidence-lower-bound scoring and an isolated exploration
budget are the architecture the user described, and exploration is explicitly deferred until there are
users. `model_metrics` still holds zero rows, so there is no production evidence for an LCB to bound — the
external prior is what would seed it, and seeding it is the next step rather than this one.

## 9. 2026-08-25 — the video router ranked models that cannot make video

Two routing defects, both found by reading `VideoModelRouter` against the registry it reads.

### A non-video model could be recommended for a video shot

`ModelCapabilityRegistry.all()` inner-joins every `ModelDefinition` to its `ModelCapabilityProfile`, across
**every modality** — chat, embedding, image, video. `VideoModelRouter._eligible` checked duration, resolution,
aspect ratio, references, trust and criticality, and never checked `modality` or `supported_operations`. A model
with no declared duration and no declared resolutions passes every one of those constraints vacuously.

Measured, not inferred. A plain `ShotRequirements()` T2V request ranked **eight non-video models**, and:

```
0.47840  embedding        openrouter:google/gemini-embedding-2
0.46592  video            wan:wan-2.7
0.46400  text_multimodal  openrouter:anthropic/claude-opus-5
0.46400  multimodal_embed openrouter:voyageai/voyage-multimodal-3.5
```

An embedding model outscored `wan-2.7` — the one model on this platform with live evidence behind it. The eight
video models above it are only above it while their providers stay configured; `production_engine` excludes any
model whose provider is unconfigured, so a credential change was enough to make an embedding model the
recommendation for a video shot.

It also reached persisted evidence. `director_production/pipeline.py` records
`"fallback_providers": [item.provider for item in prepared.router.candidates[1:]]` into the generation plan, so
every stored plan listed embedding and chat providers as video fallbacks.

The fix is the two constraints the registry already had the data for, applied **before** scoring, plus the audit
record that was missing: a rejected model is returned in `RouterDecision.rejected` with `reason_codes`, rather
than disappearing from the list. `LookupError` on an unroutable request now names the codes instead of saying
only "no active model".

### Live metrics were written onto a shared singleton

`production_engine/runtime.py` did this per request, on the container-wide router:

```python
self.router.production_adjustments = production
self.router.production_sample_counts = counts
self.router.benchmark_adjustments = self.benchmarks.adjustments()
```

Two concurrent rankings read each other's evidence, and no decision could be replayed against the evidence that
produced it. Evidence is now a frozen `RoutingEvidence` passed to `rank(..., evidence=...)`; the three attribute
names survive as read-only properties, so the old assignment raises `AttributeError` rather than silently winning
a race. `FEATURE_ADAPTIVE_ROUTER=false` limited the blast radius; it did not remove the defect.

Router version moved `video-router-v1` → `video-router-v2`. Four tests in `tests/test_model_router.py` cover the
modality gate, the rejection record, the named-reason `LookupError` and the per-call evidence isolation.

**Not done in this pass** — the rest of the routing rework (explicit `RoutingPolicy` per role, a `RoutingContext`
hash, a `RoutingPlan` with a conditional fallback graph, confidence-lower-bound scoring) is unstarted. See §16.

## 10. 2026-08-23 — the image path

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

## 11. 2026-08-23 — the media plane and the second style layer

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

## 12. Direct uploads, and layer 2 switched on

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

## 12b. Wan 2.7's three modes, and the upload state machine

### Wan 2.7 is three DashScope models

The adapter already inferred a mode from the request shape — a reference video means r2v, a
first frame means i2v, text alone means t2v — and mapped each to its own DashScope model ID.
Two of the three IDs were undated placeholders. All three are now the reviewed runtime IDs:

| Mode | Model | Accepts |
| --- | --- | --- |
| T2V | `wan2.7-t2v-2026-06-12` | text, optionally one custom audio track (`input.audio_url`) |
| I2V | `wan2.7-i2v-2026-04-25` | a first frame or first clip, a last frame, driving audio |
| R2V | `wan2.7-r2v-2026-06-12` | image/video references, optionally one first frame, plus text |

> The "Accepts" column above was wrong until §12d and is corrected here. T2V has
> no media plane at all and I2V accepts no reference image; the earlier reading
> of "optionally images" put reference stills on two modes that have nowhere to
> put them.

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
query read it. At the time of that pass it was still not swept; §12d closes that loop — the
worker now runs the sweep on an interval, and `docs/OPEN_ISSUES.md` §2.11 records the state.

Five regression tests in `tests/test_direct_upload.py` cover these; four of them fail against
the previous code.

## 12c. The Wan media plane, and single-owner completion

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
| `reference_voice` | a voice the model conditions on |

> **Superseded by §12d.** This pass concluded that the role was internal state
> and that array position was the only signal the provider received, and
> serialized `{"type": "image"}`. The published API says the opposite: `type`
> carries the role verbatim. The mode matrix below was wrong in the same pass —
> T2V and I2V were each credited with a `reference_image` neither has a field
> for. Read §12d for the current contract.

The mode matrix as it stood then, **since corrected**:

| Mode | Accepts | Framing sent |
| --- | --- | --- |
| T2V | ~~`reference_image`~~ | `resolution` + `ratio` |
| I2V | `first_frame`, `last_frame`, `first_clip`, ~~`reference_image`~~ | `resolution` |
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
defaulting false everywhere). This pass declared it `false` for Wan 2.7 on the
reading that 2.7 carries no audio asset in at all; §12d flips it to `true`,
because R2V nests a `reference_voice` audio URL inside a reference material and
I2V takes a `driving_audio` media entry. The flag's meaning — an asset the model
conditions **on**, as against `supports_audio` for audio it produces — is
unchanged and is what makes it the right flag for both.

`test_wan_declared_capabilities_are_ones_the_wire_can_actually_carry` binds the
two together in **both** directions: a flag set true with no mode accepting its
role is a promise the adapter would refuse, and a mode accepting a role no flag
claims is an input nobody authorised.

Framing was wrong too. `parameters.size` was receiving `"720p"` — a tier label
in a field that takes pixel dimensions — and `size` is not in the published
parameter set at all, so nothing sends it now. A caller asking for exact pixels
is refused rather than silently ignored.

The profile was `wan-2.7-manual-v3`, which separates continuation
(`supports_video_extension`, I2V), reference (`supports_v2v`,
`supports_reference_image`, `supports_multi_reference`,
`supports_character_reference`), voice (`supports_reference_voice`) and edit
(explicitly `supported: false`). §12d supersedes it with `wan-2.7-manual-v4`.

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
  closed and whose client never came back. It does not delete the orphaned
  object, and it *reports* stale holds rather than releasing them — a hold whose
  registration succeeded and whose settlement failed must survive, and only an
  operator can tell the two apart. This pass made it an endpoint *only*, on the
  reasoning that sweeping quota is an operator's schedule to own; §12d keeps the
  endpoint and adds the worker interval behind it, because "reclaimable" and
  "reclaimed" turned out to be two different claims.
- **One lineage key.** `media_service.lineage_key` is the single definition, and
  completion consumes the value the authorization already stored on the upload
  row. Deriving it twice made deduplication depend on two formulas agreeing.

## 12d. The Wan 2.7 wire protocol, and a sweep that actually runs

### `media.type` is the role, not a media category

The previous pass (§12c) modelled the role as canonical internal state and
serialized a media *category* — `image`, `video`, `audio` — on the theory that
array position was the only signal the provider received. The published API says
the reverse. `media.type` carries the semantic role verbatim, and nothing is
inferred from position:

```json
{"type": "first_frame",     "url": "..."}
{"type": "last_frame",      "url": "..."}
{"type": "first_clip",      "url": "..."}
{"type": "driving_audio",   "url": "..."}
{"type": "reference_image", "url": "...", "reference_voice": "..."}
{"type": "reference_video", "url": "...", "reference_voice": "..."}
```

So every Wan I2V and R2V request this platform could have sent was malformed.
`WanMedia.as_payload` is now `{"type": self.role.value, "url": self.url}`.

**The tests were the reason a green suite proved nothing.** They asserted
`{"type": "image"}` — the adapter and its tests agreed with each other and
neither agreed with DashScope. They now name the published values, and
`test_wan_declared_capabilities_are_ones_the_wire_can_actually_carry` pins the
registry's `media_types` list to the role enum rather than to a hand-written
list, so the two cannot drift again.

Ordering survives, with a different justification: position no longer carries
meaning, but a deterministic array means one request shape produces one payload,
which is what keeps idempotency keys and recorded fixtures stable.

### Audio and the negative prompt were in the wrong objects

`negative_prompt` belongs to `input`, beside `prompt`, in all three modes. It was
being sent as a `parameters` field.

`parameters.audio` does not exist in any mode. The compiler was passing
`common["audio"]` — the shot's audio *design*, a dict — and the adapter posted it
verbatim, so **every** Wan request this platform has ever built carried
`"audio": {}`. Wan 2.7's three audio inputs are all assets, and all three are
different instructions:

| Mode | Field | Means |
| --- | --- | --- |
| T2V | `input.audio_url` | a custom track laid over the result |
| I2V | `media[].type = driving_audio` | audio the performance is driven from |
| R2V | `reference_voice` on a reference entry | the timbre of that plate's subject |

They are kept apart rather than normalised: an `audio_url` on an I2V request is
refused and told to use `driving_audio`, not silently re-labelled. The design
dict is refused too — the prompt already carries the audio design in words.

`reference_voice` is a **field on a reference material**, not a media entry of
its own, which is what the previous pass's standalone role would have serialized.
A flat `reference_voice` binds to the single reference asset in the request; with
two or more it is ambiguous and the adapter says so instead of picking, because a
voice attached to the wrong plate is a billed generation with the wrong character
speaking.

### The mode matrix advertised inputs two modes have no field for

| Mode | Accepts | Framing sent |
| --- | --- | --- |
| T2V | *nothing* — `prompt`, `negative_prompt`, `audio_url` | `resolution` + `ratio` |
| I2V | `first_frame`, `last_frame`, `first_clip`, `driving_audio` | `resolution` |
| R2V | `reference_image`/`reference_video`, optionally one `first_frame` | `resolution`, `ratio` only without a first frame |

T2V's HTTP API has no media array; I2V's accepted types do not include a
reference image. Both were declared to accept one, in the adapter *and* in the
registry, and a test asserted that `mode=t2v` with reference stills was accepted
and posted — references resolved, presigned, billed, and discarded by the
provider.

Membership is not the whole rule either. I2V publishes a **closed list** of
material combinations, now held in `_I2V_COMBINATIONS` and pinned against the
registry:

```text
first_frame                         first_clip
first_frame + driving_audio         first_clip + last_frame
first_frame + last_frame
first_frame + last_frame + driving_audio
```

A last frame with nothing to grow from, or driving audio with nothing to drive,
is refused before billing rather than after. R2V requires at least one reference
material — a first frame alone is an I2V shot and is told so.

### Duration has a floor of 2 and a ceiling that depends on the request

The profile declared `min_duration: 1`. Wan 2.7's floor is 2, so a one-second
shot was routable and refused by the provider. And the ceiling is not one number:
R2V carrying a reference **video** caps at 10 seconds where everything else
reaches 15. A static `max_duration` cannot express that, so `max_duration_for()`
computes it per request and the registry records the exception on the mode.

The profile is now `wan-2.7-manual-v4`.

### The expiry sweep raced completion, and nothing ran it

Two separate problems behind one endpoint.

**It raced.** The sweep read expired rows with no lock, released the hold in one
transaction and abandoned the row in another — the opposite order from the
completion path, which locks the `DirectUpload` row first and its reservation
second. That left this open:

```text
sweeper  reads the upload, still PENDING
complete locks the upload row and begins adopting it
sweeper  releases the reservation and commits
complete calls settle_in, finds it RELEASED -> StorageReservationConflict
```

The completion rolled back and answered **500** for an upload the client had
finished correctly. `DirectUploadService.claim_expired` now takes the row lock
first, **re-reads the expiry predicate under it**, and the caller releases the
hold in the same transaction through the new `WorkspaceStorageQuota.release_in`.
One commit, or none: a release that conflicts rolls the abandon back with it and
the row stays sweepable. The completion endpoint also answers **409** rather than
500 if a settlement conflict ever arises from another cause — fail-closed
settlement should produce an answer, not a stack trace.

A `postgres_only` test forces the exact interleaving: a sweeper thread is
released at the moment the completion holds the row and is about to settle. It
blocks on the row lock, sees `COMPLETED`, and leaves. Removing the `FOR UPDATE`
makes it fail.

**And nothing ran it.** `POST /internal/maintenance/expired-uploads` existed and
no cron, worker or scheduler called it, so "stale upload expiry sweep is done"
only ever meant "the manual endpoint is done". The sweep now lives in
`media_service.maintenance.sweep_expired_uploads`, and both the endpoint and the
worker loop call that one implementation — `EXPIRED_UPLOAD_SWEEP_INTERVAL_SECONDS`
(default 300, `0` disables) and `EXPIRED_UPLOAD_SWEEP_LIMIT` (default 200). It is
due immediately on worker start, never fatal to the job loop, and safe to run
beside the endpoint or a second worker because every upload is claimed under its
own row lock.

No deployment change is needed for it: `docker-compose.yml` already runs
`video-platform-worker`, so the sweep is scheduled in the deployed stack as
soon as this code is deployed. Both settings are in `.env.example`; the defaults
apply without them.

Unchanged on purpose: the object is still never deleted, and stale `RESERVED`
holds with no `PENDING` upload behind them are still reported for reconciliation
rather than released.

### A consequence worth expecting

The compiler puts every resolved input into one Wan payload — start frame, end
frame, first clip, reference images, reference video. Against the corrected
matrix some combinations now have **no Wan mode**: a shot carrying an end frame
*and* reference stills, for instance, is I2V's bracket plus R2V's references and
neither mode takes both. Those are refused with `INVALID_REQUEST` naming what
each mode accepts, where before they were accepted and posted with half the
inputs silently discarded. The refusal is the correct answer — split the shot or
route it elsewhere — but it is a behaviour change on shots that previously
"worked", and it will show up as routing failures rather than as bad output.

### One test proved nothing and now proves the thing

`test_a_lost_authorization_insert_race_replays_instead_of_erroring` wrapped
`authorize` in a function that flipped a flag and called through unchanged. It
hid no winner, raised no `IntegrityError`, and passed whether or not the recovery
path existed. The idempotency lookup is now one seam
(`DirectUploadService._existing_authorization`, read once before inserting and
once after losing the insert), the test hides the winner from the **first** read
only, and it asserts both that the second lookup happened and that the answer
came from the replay path.

## 13. Also closed on 2026-08-23

| Was | Now |
| --- | --- |
| Style lock reached the prompt only via `prepare_autopilot`; every other caller of `compile()` silently produced prompts with no style lock | `PromptCompilerService` resolves the project lock from `ProjectStyleService` itself. A caller-supplied `style_lock` cannot override the authoritative one. `scripts/simulate_short_story.py` dropped its mirror of the old injection and is now the proof. |
| `MemoryQuery` had no `episode_id` and no recency weighting | `episode_id`, `EpisodeScope` (`EPISODE`/`SERIES`) and `recency_half_life_days`. Scoping is per layer now: L0 series-wide, L1 scene-fenced, L2 episode- or series-scoped. **L2 was previously fenced to the current scene**, so the layer whose purpose is recalling earlier work could not see any of it — that is why the 60-episode case never worked. |
| `VIDEO_GROK` / `VIDEO_VEO` kept FALLBACK bindings on transportless stubs | Both bindings removed; the model definitions stay as capability records. The integrity gate now rejects a binding of *any* kind onto a stub, not just a PRIMARY. |
| `RUNAPI_BASE_URL` / `RUNAPI_MODEL_ID` empty | Configured. `RUNAPI_IMAGE_PATH` and `RUNAPI_VIDEO_PATH` were missing from `.env` and are now present. |
| `references/` had no Wan 3.0 envelope and no Seedance 2.5 entry | Both added, plus a new `gpt-image.md`. Omni still blocked on the transport decision. |

## 14. What changed in the 2026-08-22 session

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

## 15. Files worth knowing

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

## 16. Open items

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

## 17. Git state

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

## 18. Model-backed prompt compilation (not started)

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

## 19. P2 — data and release blockers

- Migration head is `0034_narrative_ledger` with offline/temporary-database evidence only.
  Production-shaped populated upgrade and rollback are unverified.
- No live evidence for Payment, Provider, VLM, real billing or real on-chain payment.
- Email verification, MFA, member/device sessions, production secrets/HTTPS, backup and
  restore, monitoring and alerting, and operational process all still block release.
- Production visual detector / tracker / encoder and a trusted `VLM_REVIEWER` are neither
  deployed nor calibrated.

## 20. Standing rules

- Providers are offline by default; live needs all three gates plus a `LiveCanaryPermit`.
- Adapter payloads may never carry tenancy, accounting or audit fields.
- A logical model name must never reach a provider as an API model ID.
- Voyage is `ADVISORY` only — never identity, state, delta or commit authority.
- The agent does not write credential values into files.
