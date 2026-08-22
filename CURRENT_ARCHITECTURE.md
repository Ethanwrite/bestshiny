# AI Director Platform — Current Architecture

Snapshot date: 2026-08-22
Repository: `ai-director-platform`
Branch: `main`
Offline algorithm baseline: commit `0a74d31`, tag `v0.2.0-algorithm-core-offline`
Phase III implementation: commit `99f9c60`, evidence tag `v0.3.0-production-evidence-core-offline`
Migration head: `0029_project_style_lock`
Release posture: **NOT PRODUCTION-READY**

This document describes the Phase III evidence checkpoint plus the current 2026-08-22 persistent-character-state
development checkpoint. The offline baseline was frozen after the historical `348 passed, 39 warnings` gate. The tagged
Phase III checkpoint passed `406 passed, 57 warnings in 71.58s`, Mypy over 121 source files, Ruff lint, Ruff format
(226 files), Node syntax and `git diff --check`. Those numbers are historical tag evidence, not a test count for every
later working-tree edit. The current working tree passes `451 passed, 61 warnings in 88.91s`, Ruff format/check,
Mypy over 122 source files and `git diff --check`. The checkpoint remains offline evidence rather than a production
release; no real Provider call was executed, and this state milestone adds no Provider.

## Truth labels

| Label | Meaning |
| --- | --- |
| Frozen baseline | Present at commit `0a74d31` and recoverable from tag `v0.2.0-algorithm-core-offline` |
| Checkpoint implemented | Code and offline tests exist in the committed Phase III evidence checkpoint |
| Fixture evidence | Real local bytes/SQL/service flow were exercised with self-generated or deterministic test inputs |
| Live verified | A real external Provider call and its result/billing evidence were recorded |
| Not implemented | No complete current path exists |

There is **no Live verified Provider row in this snapshot**. Adapter, payload, Mock transport and fixture evidence
must not be relabeled as live proof.

## System shape

The product is a Python 3.12 modular monolith with a static responsive Web application, FastAPI control/data plane,
a durable generation worker and a Chrome extension/browser worker for explicitly authorized Google Flow sessions.
PostgreSQL + pgvector is the production database; SQLite remains a local/test compatibility target. Media uses
local or S3-compatible storage.

```mermaid
flowchart TB
  Web["Web Workbench\nPassenger + Autopilot"]
  API["FastAPI\nAuth + user + internal evidence APIs"]
  Director["Director Core\nNarrative + continuity + policy + candidate"]
  Roles["ModelRoleRuntime\nrole + plan + trust + live permit"]
  Registry["Persistent Model Registry\nModelDefinition + ModelCapabilityProfile"]
  Memory["Narrative Memory\nSQL timeline + runtime embedding"]
  State["Persistent Character State\nversion + delta + validation + commit + CAS head"]
  Style["Project Style Lock\nversion embedding + injection + drift gate"]
  QA["CharacterEvidence + QA\nlocal frames + confidence + review"]
  Visual["VisualProductionRuntime\ncontext + routing + prompt adapters"]
  Gateway["GenerationGateway\njob + paid boundary + billing evidence"]
  FlowAffinity["FlowProjectAllocator\nsticky account/project + migration plan"]
  Ledger["Workspace Credits\nReserve -> Settle/Refund/Reconcile"]
  Evidence["Production Evidence\nexecution + billing + outcome + cost"]
  Media["Media/Asset Registry\nlineage + storage quota"]
  DB[("PostgreSQL + pgvector\nor SQLite")]
  Store[("Local/S3-compatible storage")]
  Providers["External Providers\nclosed by default"]

  Web --> API
  API --> Director
  API --> Visual
  Director --> Memory
  Director --> State
  Director --> QA
  Director --> Style
  Director --> Evidence
  Memory --> Roles
  State --> QA
  State --> Director
  Roles --> Registry
  Visual --> Registry
  Style --> Visual
  Style --> QA
  Visual --> Gateway
  Gateway --> FlowAffinity
  Gateway --> Ledger
  Gateway --> Evidence
  Gateway --> Media
  Roles --> Evidence
  Gateway --> Providers
  Roles --> Providers
  Director --> DB
  State --> DB
  Style --> DB
  Registry --> DB
  FlowAffinity --> DB
  Ledger --> DB
  Evidence --> DB
  Media --> DB
  Media --> Store
```

Passenger and Autopilot share `VisualProductionRuntime`, `GenerationGateway`, `MediaRegistry`, storage, routing,
provider execution and accounting. A second generation engine or wallet is not allowed.

## Repository layers

| Layer | Principal paths | Current status |
| --- | --- | --- |
| Web | `apps/web/` | WIP implemented; cookie/CSRF client path and 4-second starter default connected |
| API | `apps/api/video_platform_api/` | WIP implemented; auth/quota/canary/evidence routes added |
| Browser worker | `apps/browser-worker-extension/`, `services/browser-runtime/` | Frozen baseline; no current Flow live validation |
| Domain/contracts | `packages/domain/`, `packages/contracts/` | WIP through migration `0029`; persistent character-state and project-style rows are schema-backed |
| Model infrastructure | `core/model-registry/`, `core/entitlements/`, `config/model-registry/` | Persistent single capability truth and role runtime |
| Director/QA/cost | `core/character/`, `core/style/`, `core/narrative/`, `core/continuity/`, `core/generation-policy/`, `core/qa/`, `core/cost/`, `core/production/` | WIP implemented with offline evidence tests, including persistent state and locked-style generation/commit gates |
| Generation/media | `services/generation-gateway/`, `services/media-service/`, `services/production-engine/` | Durable paid boundary, billing evidence, Flow affinity and storage quota |
| Providers | `providers/` | Mixed adapter/stub state; none live-verified in Phase III |
| Skills | `skills/` | Existing guidance retained; no broad Phase III expansion |

## Product entry modes and accounting

Passenger path:

```text
authenticated request
-> server-owned plan/role/capability resolution
-> server pricing and trust/criticality gates
-> atomic Job + credit reservation + CostRecord + idempotency
-> shared Visual Runtime and GenerationGateway
-> Provider boundary (Mock by default; live also requires a permit)
-> completion/billing evidence/media
-> settle, refund or reconcile
```

When Passenger video duration is omitted it defaults to 4 seconds. Under the current Seedance pricing snapshot this
is about 44 credits, allowing one request against the 50-credit starter grant. An explicit 8-second request remains
about 87 credits and fails before Job/Provider creation if the balance cannot be reserved. Purchases, recurring
grants, expiry and administrator adjustments are not implemented.

The workspace wallet is authoritative only for user credits:

```text
RESERVED --completed--> SETTLED
RESERVED --proven pre-submit terminal--> REFUNDED
RESERVED --paid result uncertain--> RECONCILIATION_REQUIRED
RECONCILIATION_REQUIRED --evidence decision--> SETTLED | REFUNDED
```

Workspace credits, generation supplier USD/credit evidence, Flow account credits and RunAPI's edge budget are
separate accounting domains.

## Authentication, tenancy and storage

Authentication provides email/password registration and login, PBKDF2-SHA256 password hashing, hashed durable
sessions, workspace roles and project/asset/job tenant isolation. Phase III adds:

- HttpOnly session cookies;
- `Secure` cookies in production and `SameSite=Lax`;
- double-submit CSRF for unsafe cookie-authenticated requests, while scoped Bearer/internal callers remain
  supported;
- persistent login throttles;
- expiring, hashed, one-use password-reset tokens; successful reset revokes active sessions;
- Web `credentials: include` and CSRF headers, with no session token in `sessionStorage`.

Workspace storage keeps `max_storage_bytes`, `used_storage_bytes` and `reserved_storage_bytes`. Upload admission
reserves bytes atomically; successful registration settles the reservation, proven failure releases it and an
uncertain post-registration failure keeps a hold for reconciliation. `MediaAsset.size_bytes` records actual bytes.

Email verification, MFA, member invitations/removal, device-session operations and a complete security-event
program remain production blockers.

## Narrative, timeline and committed-state safety

The deterministic Narrative Compiler still produces the SQL story graph and planned state. It does not require a
Provider call. External model reasoning is used only where a current business caller explicitly requests it
through `ModelRoleRuntime`.

`TimelineState` remains authoritative. `AuthoritativeTimelineStateEngine` v3 uses a relational
`TimelineTransition` for:

```text
CONTINUOUS, SCENE_CUT, TIME_JUMP, FLASHBACK, FLASH_FORWARD,
MONTAGE, DREAM, LOCATION_CHANGE, EXPLICIT_RESET
```

`CONTINUOUS` can propagate committed character/prop/costume state. Scene/location boundaries reset spatial state.
Time jumps, flash-forward and montage require reconciliation. Flashback/flash-forward/dream create branch keys.
Legacy string hints are converted to a row instead of remaining an alternate source of truth.

Editing an earlier committed-state input marks later shots `downstream_state_stale` with `RECOMPUTE_REQUIRED`.
Planning-only recompute stops at active/committed shots and never mutates committed media. The public committed-shot
revision experience is not claimed complete.

## Persistent narrative character state

`PersistentCharacterStateService` adds the missing state-transition loop without turning model output into truth.
It maintains two deliberately separate layers:

| Layer | Examples | Mutation rule |
| --- | --- | --- |
| Immutable identity | locked identity version, canonical assets, face/body proportions, canonical hair and outfit design/color | ordinary shot deltas are rejected; identity changes belong to a separate explicit identity-version/rebase workflow, which the ordinary state-delta API does not perform |
| Mutable narrative state | injury and blood state, outfit damage/contamination/wetness, held props and their state, location, time, lighting and emotional beat | may change only through a candidate-bound delta that passes policy and evidence before commit |

The mutable state is a fully materialized, hash-chained `CharacterStateVersion` scoped by
`(project_id, character_id, timeline_scope_key)`. A candidate proposes an append-only RFC 6902-style
`CharacterStateDelta` against the exact current version, identity fingerprint, branch head and input/planned-output
`TimelineState` hashes. The target state is injected into the generation specification and prompt as **proposed** state,
so the renderer receives the intended end state while SQL still keeps the previous version authoritative.

A proposal may be written only while the Candidate is exactly `CREATED` and generation has not been dispatched. It is
inserted by the Candidate/Generation Job allocation callback inside the same admission/reservation transaction. The
complete proposal-set hash is stored on the Candidate and copied to the Generation Job request in that transaction;
validate and commit rederive and compare both bindings. A late or altered proposal therefore cannot inherit evidence
from the bytes generated for a different proposal set.

```mermaid
flowchart LR
  Identity["Locked identity version"] --> Base["Committed state vN"]
  Base --> Delta["Candidate state delta"]
  Delta --> Policy["Deterministic state policy"]
  Policy --> Generate["Generate candidate with proposed target"]
  Generate --> Evidence["Output-bound visual observation"]
  Evidence --> Decision{"PASS / REVIEW / REJECT"}
  Decision -->|PASS| Adopt["Candidate adoption transaction"]
  Decision -->|REVIEW| Human["Explicit authenticated human review"]
  Human --> Adopt
  Decision -->|REJECT| Stop["Reject; head stays at vN"]
  Adopt --> Version["Append state vN+1 + commit"]
  Version --> Head["CAS branch head"]
  Head --> Timeline["Write output ref and propagate to next shot"]
```

The deterministic policy currently supports `MUST_EQUAL`, `MUST_EXIST` and `LOCK_UNTIL_SCENE`, rejects identity
paths including ancestor replacements, rejects duplicate constraint IDs, and requires observations for changed visual
paths and active visual constraints. Replacing a mutable object expands to its changed leaf paths before evidence is
calculated, so replacing `appearance.injury` or `appearance.outfit.damage` cannot hide a changed visual fact. A
high-confidence mismatch is a reject. Missing, low-confidence, advisory or untrusted evidence becomes
`REVIEW_REQUIRED`; it never silently passes. The authenticated human-review path is separate from automatic evidence
and records actor and reason. Narrative-state input/target JSON is bounded to 256 KiB, 5,000 nodes and 12 levels of
depth; a state may contain at most 200 continuity constraints.

Persistence across the normal non-initial workflow is deliberately ordered:

```text
existing append-only Delta
-> append POLICY / VISUAL / optional HUMAN_OVERRIDE validations
-> append materialized CharacterStateVersion
-> append CharacterStateCommit
-> compare-and-swap CharacterStateHead
-> write the committed version/hash into the shot output TimelineState
-> snapshot and authoritative future-shot propagation
```

The Delta is created atomically with the candidate-generation admission callback; visual validation is appended after
output evaluation. Once those rows exist, candidate adoption, new Version, Commit, head advancement, output-state
update and downstream propagation occur in one database transaction. A stale base/head, identity fingerprint,
candidate ownership or Timeline input/output hash rolls back the operation. Unchanged character bindings are also
rechecked against their current branch head before carry-forward. Version, delta, validation and commit rows are
append-only; only the head projection advances. Initial state v1 is accepted only from an already committed candidate
with explicit confirmation by an authenticated user, and its constraints are checked against the actual source-scene
sequence before the baseline is committed. Baseline initialization updates the typed character-state reference in the
authoritative output `TimelineState` and propagates it; it does not append a second untyped `ShotStateSnapshot` for the
already committed Candidate.

An explicit `TimelineTransition.branch_key` may fork from the immutable state version selected by the shot input. If
the target scope has no head, the first accepted transition is materialized as that independent scope's v1/head while
retaining the selected version/hash as its ancestor fence. The main-scope head is not advanced, and unchanged main or
historical bindings are not silently copied into the new branch scope.

The API surface is:

- `POST /v1/characters/{character_id}/narrative-state/initialize` for the explicit committed v1 baseline;
- `POST /v1/shots/{shot_id}/generate` with optional per-character `state_deltas` for a candidate proposal;
- the existing candidate validate/human-review/commit routes for the evidence and adoption stages;
- `GET /v1/projects/{project_id}/characters/{character_id}/narrative-state` for the current scoped head;
- `GET /v1/shots/{shot_id}/candidates/{candidate_id}/state-transitions` for delta/validation/commit audit.

The Mira offline fixture exercises shot 12 committed baseline, shot 13 injury blood drying/flare relocation/location
delta, deterministic locks, trusted visual observation, v2 commit and shot 14 propagation. It also rejects immutable
hair mutation and early flare ignition, routes Voyage evidence to review, rejects confident state mismatch, and blocks
stale base/timeline commits. This proves the data and transaction contract, not production VLM accuracy.

## Model capability and role runtime

`ModelDefinition`, `ModelRoleBinding` and the one-to-one persistent `ModelCapabilityProfile` now form the
authoritative model registry. The profile owns supported generation modes/references/frames/audio/text, duration,
aspect ratio, resolution, Provider metadata and manual quality priors. Old per-provider video capability JSON files
were removed, so UI, admission, policy, router, cost and adapters cannot read a parallel capability truth. Wan is
registered consistently as 2.7 rather than borrowing experimental 3.0 priors.

Manual priors are labeled `MANUAL_PRIOR`. Runtime observations are blended by the router with:

```text
prior_weight = 0.80
observation_weight = 0.20
minimum_sample_count = 20
```

The observation weight scales with sample coverage; a few attempts cannot replace reviewed priors.

All current business paths that actually execute external chat, embedding or fact-locked refinement use
`ModelRoleRuntime` (100% of current product model-execution callers). This does not mean every deterministic
Director algorithm was converted into an LLM call. Runtime execution:

1. resolves plan, role, model, trust and enabled state;
2. rechecks the persisted binding/model at the final live boundary;
3. requires a matching live canary permit in live mode;
4. executes the adapter;
5. persists success or failure as a `ModelExecutionRecord` with request hash, latency, token use and explicit cost
   source;
6. stores no prompt body or Provider key in the evidence record.

Narrative Memory no longer calls a Voyage client directly. It requests `MULTIMODAL_EMBEDDING` through the runtime,
writes `EmbeddingEvidence` containing input/vector hashes and dimension rather than the full vector, and degrades to
structured SQL timeline with `MEMORY_VECTOR_DEGRADED` when vector execution is unavailable.

Embedding provenance is now typed by `EvidencePurpose` and `AuthorityLevel`. Voyage, including
`voyage-multimodal-3.5`, is always `ADVISORY` and may be used only for retrieval hints, supporting similarity and
evidence-frame ranking. The memory boundary rejects attempts to use an embedding for `IDENTITY_VERDICT`,
`STATE_FACT_ASSERTION`, `STATE_DELTA_APPROVAL` or `COMMIT_AUTHORIZATION`. A legacy or directly inserted vector row
claiming authoritative use is excluded from retrieval. Direct video-URL embedding on the unverified runtime path also
fails closed; callers must first extract bounded timestamped frames.

## Provider status and Flow affinity

No provider below was called live during Phase III.

| Provider path | Implemented surface | Current truth |
| --- | --- | --- |
| Google Flow | BrowserRuntime, account scheduler, automatic project affinity, upload/submit/poll/download, migration plan | Offline only; default project provisioner fails closed; no real account/project operation |
| OpenRouter | Chat/responses/embeddings/video adapter and logical GPT/Claude/Kling roles | Offline/Mock only; no role canary |
| Ark / Seedance | Doubao-compatible chat and asynchronous Seedance video adapter | Offline/Mock only; no video canary |
| Wan 2.7 | OpenAI-compatible chat and DashScope T2V/I2V/R2V surfaces | Offline/Mock only; no live schema/job |
| RunAPI | Typed low-trust Edge tasks, fact lock, budget and benchmark record | Offline/Mock only; prompt canary not executed |
| DeepSeek | Compatible chat adapter | Adapter only; no default verified product deployment |
| Voyage | Runtime embedding role for advisory retrieval/frame ranking only | Offline/Mock/degraded tests only; no multimodal canary and no state/identity authority |
| Veo/Grok/Kling direct/Omni/Runway | Honest not-configured slots where applicable | Not deployed |

For Google Flow, `FlowProjectAllocator` owns first-use affinity. Active-state partial unique indexes enforce:

```text
one local project -> at most one active Google Flow binding
one remote Flow project -> exactly one permanent local owner across all binding statuses
```

The selected account is sticky. Account failure does not silently round-robin to a new context; it moves the
binding toward `MIGRATION_REQUIRED`. `FlowMigrationPlan` records source/target account and project plus
character/instruction/asset transfer facts and returns `USER_REVIEW_REQUIRED` if context cannot be verified.
Polling identifies the tuple `(local_generation_job_id, provider_account_id, provider_project_id,
provider_job_id)` rather than trusting a remote job ID alone.

## Project style lock and drift gate

`STYLE` remains an ordinary logical asset with immutable versions and explicit Canonical promotion. A project does
not follow that mutable asset pointer after confirmation: `ProjectStyleService.lock()` requires a Canonical READY
STYLE version, extracts or reuses its version-bound `StyleEmbedding`, appends one `ProjectStyleLock`, and sets
`projects.canonical_style_version_id` exactly once. Database triggers reject direct pointer writes, cross-project or
non-STYLE bindings, history edits, unlocks, and replacement locks.

Autopilot resolves the locked version even if the asset library later promotes another version. Its reference media
is placed ahead of the bounded image context; the exact version/hash and constraints enter `CanonicalShotSpec`, the
neutral/model prompt, Generation Job metadata, and each model adapter's `style_control` payload. The current offline
descriptor is a deterministic normalized 64-D color/tonal/saturation/edge/spatial vector, not a calibrated learned
model and not a Provider capability claim.

After generation, `ProjectStyleService.evaluate_candidate()` samples video positions
`0, 0.2, 0.4, 0.6, 0.8, 0.98` (or evaluates a still), persists average/minimum/p10 similarity, low-score fraction and
drift slope in `CandidateStyleEvaluation`, and returns PASS/FAIL/REVIEW_REQUIRED. `QAPipeline` consumes this evidence,
while `CandidatePipeline.commit()` independently rechecks that the immutable PASS row matches the current candidate
output, style lock, exact style version and embedding. A generic human QA approval cannot bypass that final gate.

## Character evidence and QA

`CharacterEvidenceProducer` V1 has the following local pipeline:

```text
MP4 -> FFmpeg frame sampling -> Person/Face detector interface
-> Character tracker interface -> FaceIdentityEncoder + AppearanceEncoder
-> view-aware canonical reference selection -> confidence-weighted temporal aggregate
-> QAPipeline
```

Each sample includes face visibility, detection/track confidence, yaw, blur, selected reference, face/appearance
similarity and encoder versions. Evidence quality weights low-visibility, blurred or low-confidence samples down.
Front, three-quarter and profile views select the nearest matching canonical reference. Aggregates include average,
minimum, p10, drift slope, low-score duration, appearance and reacquisition. Thresholds are versioned by shot/view
rather than learned from the current small sample.

Tracking ambiguity emits `TRACKING_UNCERTAIN` and requires semantic/VLM review. Hair and costume remain
`UNAVAILABLE`; no weak proxy is presented as high-confidence evidence.

For mutable-state facts, a trusted automatic observation must be linked to the exact candidate output asset and to a
successful same-project `ModelExecutionRecord` whose role is `VLM_REVIEWER` and whose metadata declares
`CHARACTER_STATE_FACT_OBSERVATION`. Voyage providers are explicitly excluded even if a caller relabels their result.
Missing execution provenance, a different evidence asset, advisory/low-confidence output or an unavailable reviewer
goes to authenticated human review; a confident contradiction rejects the state transition. A real production VLM
reviewer has not yet been deployed or calibrated, so the current trusted-VLM tests use controlled execution/evidence
fixtures.

Evidence validation currently uses a self-generated non-user MP4 and deterministic injected detector/tracker/
encoder implementations. FFmpeg reads real video bytes, but concrete production inference models are not bundled,
deployed or calibrated. Therefore Phase III proves the producer contract and QA evidence flow, not production
identity accuracy or a real-user visual QA result.

## Production evidence and cost

Phase III introduces these durable evidence rows:

| Evidence | Purpose |
| --- | --- |
| `ModelExecutionRecord` | role/model/provider attempt, hash, latency, tokens, status and cost source |
| `EmbeddingEvidence` | asset/input/model linkage, dimension, vector hash, latency and optional cost |
| `ProviderBillingEvidence` | Provider cost/credits with `VERIFIED_PROVIDER`, `ESTIMATED`, `RECONCILED_MANUAL` or `UNKNOWN` |
| `DecisionOutcomeRecord` | shot features, continuity, generation policy, provider/model, candidate, QA, user outcome and cost |
| `RunAPIBenchmark` | edge task hash, fact-lock/fallback, latency, quality, actual cost and optional acceptance |
| `LiveCanaryPermit` / `LiveCanaryUsage` | bounded live authorization and each reserved/uncertain/settled operation |

Gateway completion parses a constrained set of Provider usage/billing fields and stores a response hash/reference,
not the raw response. When no trustworthy amount or credits exist, evidence is `UNKNOWN`, estimates remain separate
and `actual_cost` stays null. Verified or manually reconciled amounts retain their source.

Accepted-shot economics include failed candidates, the accepted candidate and repair/retry attempts. Aggregation
reports attempts, accepted count, QA/identity/camera/action pass rate, latency, failure rate, verified/estimated
totals, wasted cost and cost per accepted shot. `DecisionOutcomeRecord` is the durable future training/evaluation
join and is not casually deleted.

## Live-call safety

Default execution is offline:

```env
PROVIDER_MODE=mock
ALLOW_LIVE_PROVIDER_CALLS=false
```

A normal live call requires the existing three-part gate:

```env
PROVIDER_MODE=live
ALLOW_LIVE_PROVIDER_CALLS=true
LIVE_PROVIDER_CONFIRMATION=I_UNDERSTAND_THIS_COSTS_MONEY
```

RunAPI also requires `ALLOW_RUNAPI_EDGE_CALLS=true` and Edge/criticality/budget approval. Phase III adds a mandatory
durable `LiveCanaryPermit` at both `ModelRoleRuntime` and media-generation boundaries. A permit binds Provider and
model, expiry, purpose, maximum request count and maximum USD cost. Reservation is idempotent; the remote boundary
is marked uncertain before network work; proven pre-boundary failure may release; a trusted actual amount settles;
request or cost exhaustion hard-stops.

`POST /internal/live-canary-permits` requires `PLATFORM_API_KEY`, explicit confirmation and an idempotency key. It
only creates authorization and audit data; it never executes a Provider. No canary permit was used for a real call
in this phase.

Actual Phase III canary status:

```text
RunAPI edge:        NOT EXECUTED
OpenRouter role:    NOT EXECUTED
Voyage embedding:  NOT EXECUTED
Flow low-cost:      NOT EXECUTED
Single video shot: NOT EXECUTED
Known spend:        USD 0
```

## Data architecture and migrations

Phase III extends the existing table groups with:

| Domain | Tables/columns |
| --- | --- |
| Flow affinity | enriched `provider_projects`, `flow_migration_plans`, `generation_jobs.provider_project_id` |
| Capability | `model_capability_profiles` |
| Execution/evidence | `model_execution_records`, `embedding_evidence`, `provider_billing_evidence`, `decision_outcome_records`, `runapi_benchmarks` |
| Timeline | `timeline_transitions`; Shot stale/recompute columns |
| Canary | `live_canary_permits`, `live_canary_usages` |
| Auth | `password_reset_tokens`, `auth_login_throttles`; credential status/fingerprint fields |
| Storage | workspace max/used/reserved bytes, `storage_reservations`, `media_assets.size_bytes` |
| Persistent character state | append-only `character_state_versions`, `character_state_deltas`, `character_state_validations`, `character_state_commits`; mutable CAS projection `character_state_heads` |
| Project visual style | `projects.canonical_style_version_id`; append-only `style_embeddings`, `project_style_locks`, `candidate_style_evaluations` |

The migration chain is single-head through:

```text
0024_workspace_credit_lifecycle
-> 0025_flow_project_affinity
-> 0026_model_capability_registry
-> 0027_production_evidence_core
-> 0028_persistent_character_state
-> 0029_project_style_lock
```

PostgreSQL 17.10 + pgvector 0.8.6 was validated on temporary databases for fresh and populated paths, supported
round trips, `vector(16)`, indexes/unique constraints/foreign keys, credit reservation transactions, generation
enqueue transactions and the tagged Phase III head `0027`. Migration `0028` adds database checks/triggers for project,
character, identity, candidate, timeline, validation and commit ownership; immutable history; forbidden identity keys;
commit evidence; and head fencing on both SQLite and PostgreSQL code paths. Dedicated SQLite schema/migration cases and
positive/negative trigger cases on a fresh temporary PostgreSQL 17 instance pass for `0028`. This is development
evidence, not proof that the historical Compose volume or an existing production database has been upgraded. The
ignored `data/platform.db` is not used as production migration evidence and must not be blindly stamped or upgraded.

Migration `0029` is the current code head and adds a one-time, exact-version project style pointer plus immutable
embedding, lock, and candidate evaluation rows. SQLite migration/schema regression is covered. No PostgreSQL 17,
Compose populated-upgrade, real learned style encoder, or Provider style-control canary evidence is claimed for
`0029` yet.

## Internal observability

`GET /internal/production-evidence` is protected by `PLATFORM_API_KEY` and requires an exact `project_id`, with
optional job/shot filters. It returns redacted model executions, Provider jobs, Provider billing evidence,
CostRecords, Flow bindings, QA evidence, decision outcomes, timeline transitions and stale state. Provider
references are fingerprinted; prompt bodies, vectors, raw Provider responses and credentials are not returned.

`POST/GET /internal/live-canary-permits` creates explicitly confirmed, idempotent permits and lists permit/usage
state. These are development/operator APIs, not a redesigned analytics dashboard.

Authenticated narrative-state read and audit APIs expose state hashes, lineage, patches and validation decisions but
never grant callers the ability to mark embedding output authoritative. State initialization and human confirmation
reject the development-auth bypass so actor provenance cannot be fabricated.

## Deployment evidence

Docker Desktop 29.5.3 was used for an offline production-like smoke with development-only fake credentials:

- `docker compose config -q` passed;
- API, worker and Web images built;
- Compose started successfully;
- PostgreSQL, MinIO and API reported healthy; Web and worker stayed Up;
- MinIO initialization exited 0 and created the bucket;
- host checks returned HTTP 200 for API `/health`, Web `/` and MinIO live health;
- in-container Alembic `current` was head `0027`, `alembic check` found no schema drift except the known FK-cycle
  warning, and pgvector reported 0.8.6;
- no Provider key was supplied and no live Provider call was possible.

The stack is shut down after the smoke without deleting volumes. This is local deployment evidence for the tagged
  `0027` checkpoint, not evidence that `0028` or `0029` has run in that Compose stack, and not evidence for managed secrets,
HTTPS, backups, external observability or a public production environment.

## Current release posture

The Phase III checkpoint is not ready for production despite offline, PostgreSQL and Docker gates passing. Remaining
blockers include:

1. deploy and calibrate concrete character detection/tracking/face/appearance inference and a trusted
   `VLM_REVIEWER` state-observation path; keep absent/untrusted provenance fail-closed to human review;
2. execute separately authorized Provider canaries and collect real billing/credit evidence;
3. keep the single paid video canary at **NOT EXECUTED** until a precise bounded permit is intentionally created;
4. complete email verification, MFA/invitations/device sessions, production HTTPS/secrets, backup/restore,
   monitoring/alerts and operations policy;
5. implement purchases/grant lifecycle/expiry/admin adjustments before claiming a complete commercial wallet.

No further Provider integration is required for the persistent-state milestone. The remaining visual blocker is a
trusted, calibrated implementation behind the existing reviewer contract, not permission to treat Voyage embeddings
or another retrieval provider as state authority.

Credential values remain outside the repository. The operator explicitly decided that the current Provider keys
do not require rotation. This decision removes rotation as a blocking action but does not authorize committing,
logging, exposing or automatically using those keys.
