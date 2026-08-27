# AI Director Platform — Current Architecture

Snapshot date: 2026-08-26
Repository: `ai-director-platform`
Branch: `claude/production-readiness-postgres` (working tree clean; pushed to `origin`)
Commit: `64ee277` — open on [PR #1](https://github.com/Ethanwrite/bestshiny/pull/1)
Offline algorithm baseline: commit `0a74d31`, tag `v0.2.0-algorithm-core-offline`
Phase III implementation: commit `99f9c60`, evidence tag `v0.3.0-production-evidence-core-offline`
Migration head: `0049_live_canary_status`
Release posture: **NOT PRODUCTION-READY**

This document describes the Phase III evidence checkpoint plus the current 2026-08-22 persistent-character-state
development checkpoint. The offline baseline was frozen after the historical `348 passed, 39 warnings` gate. The tagged
Phase III checkpoint passed `406 passed, 57 warnings in 71.58s`, Mypy over 121 source files, Ruff lint, Ruff format
(226 files), Node syntax and `git diff --check`. Those numbers are historical tag evidence, not a test count for every
later working-tree edit. The current working tree passes `610 passed, 2 skipped, 61 warnings`, Ruff check,
Mypy over 133 source files, Web production build and npm audit; the two skipped are opt-in live image tests. The checkpoint remains offline evidence rather than a production
release; no real Provider or chain call was executed, and this checkpoint adds no visual-generation Provider.

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
  RouterObs["Router Observations\nconditions + outcomes, one per attempt"]
  Layers["External Evidence Layers\nofficial | benchmark | community"]
  Posterior["Offline Posterior + Replay\nrun by hand, never in a request"]
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
  Visual --> RouterObs
  RouterObs --> DB
  RouterObs -. "offline only" .-> Posterior
  Layers -. "refused: no calibration bridge" .-> Posterior
  Posterior -. "LCB flag OFF" .-> Visual
```

Everything below `Visual --> RouterObs` is dotted for a reason. Observations flow *out* of the
request path; the posterior is computed by an operator running a script and never inside a
generation; the external layers are offered to it and refused for want of a calibration bridge; and
the only edge back into routing is behind `feature_router_lcb`, which is off. With the flag off that
return edge does not exist at runtime — the router receives byte-for-byte the evidence it received
before any of this was added.

Passenger and Autopilot share `VisualProductionRuntime`, `GenerationGateway`, `MediaRegistry`, storage, routing,
provider execution and accounting. A second generation engine or wallet is not allowed.

## Repository layers

| Layer | Principal paths | Current status |
| --- | --- | --- |
| Web | `apps/web/` | WIP implemented; cookie/CSRF client path and 4-second starter default connected |
| API | `apps/api/video_platform_api/` | WIP implemented; auth/quota/canary/evidence routes added |
| Browser worker | `apps/browser-worker-extension/`, `services/browser-runtime/` | Frozen baseline; no current Flow live validation |
| Domain/contracts | `packages/domain/`, `packages/contracts/` | WIP through migration `0033`; DePay checkout sessions, callback receipts and payment-ledger rows are schema-backed |
| Payments | `core/payments/`, `apps/web/wallet.js` | DePay shared-link QR checkout, signed callback posting and authenticated Alchemy purchase/reorg reconciliation are implemented offline; real payment is not yet executed |
| Model infrastructure | `core/model-registry/`, `core/entitlements/`, `config/model-registry/` | Persistent single capability truth and role runtime |
| Router evidence | `core/router-evidence/`, `config/router-evidence/`, `core/external-evidence/` | Four isolated evidence layers, offline hierarchical posterior, replay harness; LCB flag off, exploration has no call site, zero production observations |
| Director/QA/cost | `core/character/`, `core/style/`, `core/narrative/`, `core/continuity/`, `core/generation-policy/`, `core/qa/`, `core/cost/`, `core/production/` | WIP implemented with offline evidence tests, including persistent state and locked-style generation/commit gates |
| Generation/media | `services/generation-gateway/`, `services/media-service/`, `services/production-engine/` | Durable paid boundary, billing evidence, Flow affinity and storage quota |
| Providers | `providers/` | Mixed adapter/stub state; none live-verified in Phase III |
| Skills | `skills/`, `core/skills/` | Shared filesystem Registry and content-hash versions implemented; all twelve Skill bodies rewritten against the current contracts, none yet executed by a model |

## Unified Prompt and Skill boundary

The current working tree has one Prompt Compiler implementation and one filesystem-authoritative Skill Registry:

```text
CanonicalShotSpec
-> PromptCompilerInput { shot_spec, asset_bindings, continuity_context }
-> PromptCompilerService + SkillRegistry.resolve("prompt-compiler")
-> PromptCompilerOutput (exactly eight fields)
-> Model Router -> Video Adapter
-> GenerationRequest.provider_payload
-> GenerationGateway asset resolution
-> Provider adapter
```

`core/skills/skill_core/compiler.py` is only a compatibility import and `VideoShotPromptCompiler` is only an alias;
neither is a second implementation. The Container constructs a single `prompts` service. Skill versions are derived
from the SHA-256 hash of the complete `SKILL.md`, and PromptCompilation records the resolved hash plus typed input and
output. The database `Skill`/`SkillVersion` models are not synchronized or consumed and must not be treated as a second
active registry.

Prompt compilation is still deterministic. `skill_contract()` exposes the installed Skill text and JSON Schemas
but does not invoke `ModelRoleRuntime`, so no Skill body currently reaches a model. All twelve bodies were
rewritten on 2026-08-22 against the contracts in force: the installed `prompt-compiler` Skill now describes the
`PromptCompilerInput` envelope, the real `CanonicalShotSpec` field names, the eight `PromptCompilerOutput` fields
and the `COMPILED`/`NOT_COMPILABLE` invariants, and it emits no Provider or model selection.
`scripts/review_skill_contract.py` checks a candidate against those criteria without installing anything, and
`tests/test_installed_skills.py` keeps the structural invariants green. Enabling model-backed compilation
remains a separate, undecided step: it requires an explicit product decision on fallback behaviour, recorded in
`HANDOFF.md` section 8.

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
about 87 credits and fails before Job/Provider creation if the balance cannot be reserved. Fixed-offer purchases are
implemented; recurring grants, expiry and administrator adjustments are not.

**Every plan draws on that wallet.** Reservation used to return an inert charge for any tier other than FREE,
so a PRO or ENTERPRISE generation was priced, the quote was written onto the Job, and nothing was reserved,
settled or held for reconciliation. Who pays is one property — `WorkspaceCreditBalance.billable` — read by
both the credit service and the Gateway: every plan does; a project with no workspace and the `ALL` workspace
do not. `ALL` is the workspace created when authentication is disabled — the local development bypass, not a
tier — and it still receives server pricing and CostRecords. A plan sets the grant, the discount and which
models may be used; it does not decide whether a generation costs anything. Running out of credits answers
`402`, distinct from the `403` of a plan entitlement denial: top up versus upgrade.

The workspace wallet is authoritative only for user credits:

```text
RESERVED --completed--> SETTLED
RESERVED --proven pre-submit terminal--> REFUNDED
RESERVED --paid result uncertain--> RECONCILIATION_REQUIRED
RECONCILIATION_REQUIRED --evidence decision--> SETTLED | REFUNDED
```

Workspace credits, generation supplier USD/credit evidence, Flow account credits and RunAPI's edge budget are
separate accounting domains.

Base Native USDC purchases form another explicit evidence domain. One reusable DePay Payment Link is fixed at 30
Native USDC with quantity disabled. Every authenticated click creates a separate `OnchainPaymentIntent` for 3,000
credits, plus a hashed checkout token, and injects both `order_ref` and the opaque token into the shared link. A DePay
callback must pass RSA-PSS verification over the raw body and match the PaymentIntent, configured link ID, Base
network, Circle Native USDC contract, treasury address and exact 30 USDC. The same transaction then changes a FREE
workspace to PRO when needed and appends 3,000 credits; an existing PRO workspace receives only the credits. Business
entitlements come from the PaymentIntent snapshot, not an amount-to-credit calculation. There is no subscription,
renewal, user-wallet binding or per-order unique amount. Alchemy remains an independent authenticated chain observer:
it attaches canonical log evidence and can post append-only reorg reversal entries when the available balance permits;
otherwise the payment becomes `RECONCILIATION_REQUIRED`. Real-wallet payment evidence remains outside this checkpoint.

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

## Series-level narrative ledger

`TimelineState` carries physical state and `CharacterStateVersion` carries appearance and condition. Neither
carries **knowledge**, and neither records what the series still **owes** the viewer. Migration `0034` adds three
append-only tables plus `core/narrative-ledger/narrative_ledger_core`:

| Table | Contents |
| --- | --- |
| `narrative_facts` | A story fact, hashed, with the episode and shot that established it |
| `narrative_disclosures` | One row per (fact, holder). `holder_key` is a character ID or `AUDIENCE` |
| `narrative_obligations` | A setup and whether it is `OPEN`, `SETTLED` or `ABANDONED` |

Establishing a fact discloses it to the audience only; a character must be disclosed to separately, and
`assert_may_act_on()` fails closed when a shot would let a character act on something never disclosed to them.
Audience knowledge alone never authorises a character — that gap is what dramatic irony is, and collapsing it is
the classic long-form failure.

Obligations exist because an obligation is *owed*, not *similar*: episode 60's payoff shares no vocabulary with
episode 7's promise, so embedding retrieval can never surface it. They are carried explicitly instead.

`series_context(project_id, episode=N)` returns known facts per holder, audience-only facts and open obligations
through one bounded query. It is **O(1) in episode count** — heads, not history — which is what keeps a
60-episode arc tractable. Its output renders directly into `PromptCompilerInput.continuity_context.facts`, and
since the compiler contract admits nothing into `continuity_assertions` that was not supplied there, an
undisclosed fact cannot reach a prompt by accident.

`MemoryQuery` carries `episode_id`, an `EpisodeScope` of `EPISODE` or `SERIES`, and a per-query
`recency_half_life_days`. Scoping is applied per layer, because the layers answer different questions: L0
canonical truth is series-wide and never narrowed; L1 is *current state* and stays fenced to the current scene,
since inheriting another scene's would be wrong; L2 is "what happened before" and is scoped to the episode or
to the series. L2 was previously fenced to the current scene, which left the layer whose purpose is recalling
earlier work unable to see any of it — the reason the 60-episode case did not work. Under `SERIES` the current
episode is ranked up through an `episode_match` component rather than left to compete on cosine similarity
alone. `prepare_autopilot` retrieves with `SERIES` scope against the shot's own episode.

Known gap: retrieval is still keyed on the current shot's prompt text, so *which* earlier beat matters is
decided by similarity. Obligations are covered by the ledger; episodic callbacks are not.

## External Evidence Registry

`config/external-evidence/registry-v1.json` records what the public record says about the exact model
versions this platform runs, and — more importantly — what it does not say. It is versioned
(`external-evidence-v1`, frozen 2026-08-25), every number keeps the scale it was measured on, every record
cites its sources, and every binding to a model here declares a `version_match`.

Only `EXACT` or `EXACT_VERSION_UNSPECIFIED_REVISION` matches from grade A or B sources, at mapping
confidence above LOW, may influence a routing score. A record's grade is the **weakest** source it cites.
Near-miss evidence — Wan 2.1's diagnostics against Wan 2.7, Seedance 2.0's against 2.5, Veo 3.1 Fast's
against plain 3.1, GPT-4o's GenEval against GPT Image 2 — is deliberately recorded and bound to the model
it would tempt someone to attach it to, marked as a mismatch, because deleting it only means it gets
re-derived later and attached silently.

Nothing in the registry fuses, averages or ranks across sources: a Likert 3.75, an Elo 1154 and a 0-1
automatic 0.939 measure three different things. Human and automatic judge scores on the same benchmark
dimension are stored separately. Aggregates are stored at `mapping_confidence: LOW` and can never stand in
for a capability. A source that published words instead of numbers stores a null value.

**A retired model keeps its verdict.** `lifecycle` (`ACTIVE`/`RETIRED`), `retired_on`,
`retirement_reason` and `superseded_by` were added on 2026-08-27 when `grok-video-official` and
`veo-3.1-quality-official` stopped being executable. Execution and provenance are different facts: a
model can stop being routable without its verdict changing, and what was looked for, in which source,
against which version stays exactly as recorded. Deleting the rows to match the current routing table
would quietly rewrite history — the opposite of what an evidence registry is for. `superseded_by`
names the canonical route that took over.

This registry is **not** superseded and **not** merged into the four-layer system added on
2026-08-26 (see *Router evidence* below). They are separate frozen artefacts with separate loaders:
`registry-v1.json` is the 2026-08-25 research bound to `capability_prior`, and
`config/router-evidence/*.json` is the 2026-08-26 research keyed for the offline posterior. Nothing
reads both, which is the same rule the four layers apply to each other.

Today ten of the twelve generative models here have no diagnostic external evidence — see
`docs/OPEN_ISSUES.md` §2.25. `FEATURE_EXTERNAL_PRIOR` is **false** by default: the registry is a read-only
data asset and `GET /internal/models/external-evidence` is how it is read. Its immediate use is to say which
`capability_prior` values are backed by public evidence and which are hand-authored judgement.

## Router evidence: four layers and an offline posterior

Added 2026-08-26, reviewed and corrected 2026-08-27, documented in full in
[`docs/ROUTER_EVIDENCE.md`](docs/ROUTER_EVIDENCE.md). Truth label: **Fixture evidence** for the
posterior and replay machinery (exercised against synthetic history); the three external layers are
real public evidence with recorded provenance; there is **no production data at all** yet.

**Four kinds of evidence that fail differently, kept apart.** A vendor's own number is optimistic in
a predictable direction; a benchmark is honest about a task that may not be yours; a community
report is real experience with an unknown denominator; only production observations describe what a
user here will get. `official_prior`, `benchmark_prior` and `community_prior` are three frozen files
under `config/router-evidence/` with one loader each, and `EvidenceLayerStore` deliberately exposes
no `all_records()` and no `merged()` — a test asserts the absence, because the one thing that store
must never make easy is a list with a benchmark score and a Reddit comment in it.
`production_posterior` is computed from the `router_observations` table.

**The isolation key is the unit of everything.** `provider · model_id · exact_version · task_type ·
scenario · metric_scale_id`, plus a `ConditionBucket` of duration bucket, resolution and reference
mode for production. Two numbers may only combine when those match. `EvidenceKey.token` and
`ConditionBucket.token` are `|`-joined and round-tripped through the database, so `resolution` is
constrained to exclude that separator: a label containing one would not corrupt a single cell, it
would raise out of the whole posterior computation.

**`exact_version` pins our configuration, not always the provider's weights.** It is
`ModelCapabilityProfile.version` (`wan-2.7-manual-v4`). Where a provider's model id carries a dated
snapshot (`doubao-seedance-2-5-260628`) the pair really does identify the weights; where the id is a
repointable alias (`google/veo-3.1`) it does not, and `model_is_alias` quarantines such an
observation rather than attributing it. No provider wired here reports a resolved snapshot, so that
flag is currently never set — recorded as OPEN_ISSUES §2.31.

**Production observations are wider than `model_metrics`, which is unchanged** and still feeds the
adaptive router. `router_observations` carries the generation conditions and every observed
outcome — delivery, what the human did next, and the automated quality checks. `None` means *not
observed* and never zero, so a shot nobody rated does not become a one-star and a generation quoted
at zero credits is recorded as costing zero rather than as having no cost observed. A check
constraint refuses a failed generation carrying a quality score: otherwise a provider outage reads
as a quality problem and teaches the router to avoid a good model permanently.

**Attribution is decided when the model is chosen, not when the outcome arrives.**
`prepare_autopilot_generation` stamps a `routing_context` onto the request — task, scene, reference
mode, conditions, criticality and the exact version of the model it picked — and
`VisualProductionRuntime.evaluate_job` reads it back. The shot spec can be edited between the two,
and an observation filed under the scene the shot *became* would be attributed to a cell that never
ran. A retry is the hard case and was wrong until the review: `_execute_retry` builds its metadata
from `{**metadata, ...}`, and `RetryPlan` exists to re-route to a *different* model, so the context
carried the previous target's version onto the new one and produced observations keyed to a
provider/model/version triple that never existed. `_retargeted_routing_context` now re-resolves the
version from the registry whenever the target changes, and returns nothing when the registry cannot
name the new target — the recorder then skips, because no observation beats a mislabelled one.

**The write boundary refuses rather than coerces.** An alias binding, a missing version, or an
`observation_id` too long for its column all raise `UnattributableObservation` instead of being
defaulted, truncated or replaced. Silently rewriting an identifier is the one failure nothing
downstream can detect. Writes are idempotent on `generation_job_id`, so a retried worker or a
replayed webhook cannot inflate the counts the LCB gate reads. Collecting evidence is never allowed
to fail the user's request: the recorder absorbs the contract's own refusals *and* database errors,
which a `ValueError`-only catch did not — a PostgreSQL serialization failure under concurrent
evaluation would have failed a request whose generation had already succeeded and been billed.

**The posterior is Beta, hierarchical and offline.** A fixed Jeffreys global prior, then exact
version, task, scenario and condition bucket, each level shrinking the next by 4–6
pseudo-observations *plus* the global floor. The floor matters: without it a cell whose parent is
already near certainty inherits a near-zero pseudo-count on one side, and thirty consecutive
successes produce a Beta with `b` below 0.01 and an interval of `[0.99999, 1.0]`. There is no level
above the exact version, and the global level is a *constant* rather than an estimate over other
models — together those are the mechanical guarantee that one version's data cannot move another's,
rather than a discipline someone has to remember. Cost and latency have no posterior; they are
unbounded, and are summarised in their own units with nearest-rank percentiles.

**External evidence reaches the production posterior only through a calibration bridge, and there
are none.** `calibration.BRIDGES` is empty, so all 112 eligible external priors are refused with
`NO_CALIBRATION_BRIDGE` and every refusal is reported — a silent empty result looks identical to
"there was no evidence". The three external layers are therefore a reporting surface today, read at
`GET /internal/models/router-evidence`. They are still the answer to "why is this model's
`capability_prior` a hand-authored number", which is the question the older frozen
`registry-v1.json` was built for and which this supersedes in scope without replacing.

**`feature_router_lcb` is false, and `VideoModelRouter` is untouched.** The conservative lower bound
is delivered through the per-request `RoutingEvidence` the router already accepted, so with the flag
off the router receives byte-for-byte the evidence it received before this existed; a test pins the
router's version string, `rank`'s signature and its four scoring profiles. Three fallbacks return
the caller's evidence object unchanged: flag off, no snapshot saved, no cell sufficient (≥20
observations, ESS ≥10, interval width ≤0.55). Where two outcomes would touch one router dimension
the more pessimistic wins, and the backing count handed to the router is the **smallest** across the
adjusted dimensions — the router reads that number to weight the production term, and reporting the
largest would apply one dimension's 300 observations to another's 25.

**The offline/online boundary is a cached snapshot, not a query.** `_conservative_lcb_lookup`
materialises one posterior run and rate-limits even the check for a newer one to
`_LCB_SNAPSHOT_TTL_SECONDS` (60s). Before the review it issued a `router_posteriors` query per
routed generation, which put the offline artefact back in the hot path — the one thing the split
exists to prevent. `latest_posterior_run_id` orders by `created_at`, the row's insert time; it
previously tiebroke on `id`, a uuid4, so which snapshot the router read was not reproducible.

**Enabling the LCB requires a replay on file that passed.** `ReplayHarness` splits history
chronologically, fits on the earlier window and scores on the later one, comparing policies on
context buckets because only the arm that actually ran has an outcome. It checks interval coverage
against the **posterior predictive** interval rather than the posterior — the observable is a mean
over a finite window and carries its own sampling error on top — and gates on regret, failure rate,
cost tolerance, coverage calibration and unscored-context share together, so a policy cannot buy
quality with money or with reliability. The router key has no room for a version, so a model that
ran under more than one exact version in the fit window is replayed on the latest and the result
says which models that applied to: the one place this system has to collapse a distinction the rest
of it refuses to.

**Exploration has no feature flag and no call site.** It ships as an offline simulator with six
constraints — budget, criticality ceiling, per-generation cost, minimum evidence, failure-rate
ceiling, and an allowlist of exact *versions* rather than model ids so a silent snapshot change
cannot inherit permission. A flag is something someone can turn on; the absence of a call site is
not, and a test walks the AST of `services/`, `apps/`, `agents/` and `providers/` to keep it that
way. The simulator holds no state between runs, so repeated simulation is reproducible.

**Research is delegated; validation is not.** `scripts/research_router_evidence.py` drives the Grok
CLI in two stages — search in prose, then structure with the web disabled — and this repository only
validates. `EvidenceIngestor` refuses an unquoted number, an unregistered scale, a value outside its
own declared scale, an unattributed sample size and a version the source never named; it marks and
keeps aliases and near-miss versions, because a deleted one gets re-derived from the same page later
and attached silently to the wrong model. The quote check is the one the design leans on hardest and
was the loosest: scaling a 0-1 value by a hundred turned 0.87 into "87" and matched "we ran 87
prompts", so a sentence about sample size validated a claimed score. A scaled match now counts only
when the number in the quote reads as a percentage — a `%` or a fractional part.

## Model capability and role runtime

`ModelDefinition`, `ModelRoleBinding` and the one-to-one persistent `ModelCapabilityProfile` now form the
authoritative model registry. The profile owns supported generation modes/references/frames/audio/text, duration,
aspect ratio, resolution, Provider metadata and manual quality priors. Old per-provider video capability JSON files
were removed, so UI, admission, policy, router, cost and adapters cannot read a parallel capability truth. Wan is
registered consistently as 2.7 rather than borrowing experimental 3.0 priors.

`VideoModelRouter` ranks **video generators only**. The registry is one table across every modality, and
`registry.all()` joins all of them, so the router declares `modality = "video"` and
`required_operation = "video_generation"` and applies both as hard constraints before any score exists. A model
that fails a hard constraint is not dropped silently: it is returned in `RouterDecision.rejected` as a
`RejectedModel` carrying machine-readable `reason_codes` (`MODALITY_MISMATCH`, `VIDEO_GENERATION_UNSUPPORTED`,
`RESOLUTION_UNSUPPORTED`, `PROVIDER_TRUST_INSUFFICIENT`, `EXCLUDED_BY_CALLER`, …), so "why was that model not
chosen?" is answerable after the fact rather than by re-deriving the decision.

Measured evidence is passed **per ranking** as a frozen `RoutingEvidence`, never written onto the router. The router
is a container singleton shared by every concurrent request; while live metrics were assigned onto it, the
adjustments in force during any one ranking were whatever the previous caller happened to leave behind, and no
decision could be replayed. `benchmark_adjustments`, `production_adjustments` and `production_sample_counts` are now
read-only properties over the baseline evidence, so an assignment raises instead of quietly winning a race.

Manual priors are labeled `MANUAL_PRIOR`. Runtime observations are blended per ranking with:

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

### Model identity, and the duplicates that could never have worked

The registry holds **24 canonical models, 21 enabled** (11 video, 3 image, 7 text-multimodal, and one
each of text, embedding and multimodal-embedding). Every `provider_model_id` now carries a
`provider_model_id_source` naming the vendor page it was read from and the date.

Four IDs were wrong because nobody had checked them against the provider's own documentation.
`seedance-2.5` was a *logical name* posted to Ark, which answered "the model or endpoint does not
exist"; `grok-video` is published as `grok-imagine-video`; `veo-3.1-quality` is a Google **Flow UI
label**, not an API id, and the API publishes `veo-3.1-generate-preview`; `wan-3.0` is published as
`wan3.0-video`. `NARWHAL` is left exactly as it is and recorded as *unverifiable* — it appears on no
Google-published page, and substituting a plausible replacement would be the same error pointed the
other way.

`grok-video-official` and `veo-3.1-quality-official` are **retired**. Each held a
`(provider, provider_model_id, modality)` triple that an OpenRouter record already owned, behind a
provider whose every call raised `PROVIDER_NOT_CONFIGURED` — and `model_definitions` is UNIQUE on
that triple, so they could never have been repointed at a working transport. They were not
under-configured; they were unreachable by construction. Execution retargets to the canonical
OpenRouter routes.

`wan-3.0-openrouter` (`alibaba/wan-3.0`) is added, verified against OpenRouter's own
`GET /api/v1/videos/models` and `/api/v1/models/alibaba~wan-3.0/endpoints`. It is enabled and
declares 30s, which restores a long-form route: `wan-3.0-official` on DashScope stays a separate
record and stays disabled, because this account has no Wan 3.0 access there.

## Provider status and Flow affinity

No provider below was called live during Phase III.

| Provider path | Implemented surface | Current truth |
| --- | --- | --- |
| Google Flow | BrowserRuntime, account scheduler, automatic project affinity, upload/submit/poll/download, migration plan | Offline only; default project provisioner fails closed; no real account/project operation |
| OpenRouter | Chat/responses/embeddings, the synchronous Image API (`openai/gpt-image-2`), video adapter, and logical GPT/Claude/Kling roles | Offline/Mock only; no role canary. An opt-in live image test exists but has never been run. |
| Ark / Seedance | Doubao-compatible chat and asynchronous Seedance video adapter | Offline/Mock only; no video canary |
| Wan 2.7 | OpenAI-compatible chat and DashScope T2V/I2V/R2V surfaces | Offline/Mock only; no live schema/job |
| RunAPI | Typed low-trust Edge tasks, fact lock, budget and benchmark record | Offline/Mock only; prompt canary not executed |
| DeepSeek | Compatible chat adapter | Adapter only; no default verified product deployment |
| Voyage | Runtime embedding role for advisory retrieval/frame ranking only | Offline/Mock/degraded tests only; no multimodal canary and no state/identity authority |
| Veo/Grok/Kling direct/Omni/Runway | Honest not-configured slots where applicable | Not deployed |

### Provider reference and payload boundary

A provider declares how it accepts local media through `GenerationProvider.reference_mode`:

| Mode | Providers | Gateway behaviour |
| --- | --- | --- |
| `PROVIDER_MEDIA_ID` | Google Flow, and the default for any provider that ingests uploads | `resolve_provider_media()` uploads once per asset/provider/account tuple and emits `start_frame_provider_media_id`, `end_frame_provider_media_id` and `reference_provider_media_ids` |
| `FETCHABLE_URL` | Seedance/Ark, Wan, OpenRouter, RunAPI | No upload boundary is crossed; the Gateway resolves a short-lived **object-storage** URL and emits `start_frame_url`, `end_frame_url` and `reference_urls` |

### The application is not in the media path

A `FETCHABLE_URL` reference is a presigned credential issued by the storage backend, computed
per submission and never persisted. It is deliberately *not* `MediaAsset.public_url`: that field
addresses `/v1/storage/{key}` on this service, behind `Depends(auth.current_user)`. An external
provider cannot authenticate to it, and if it could, every reference byte would be read from
object storage into the API process and streamed back out — a dozen concurrent 4K reference
edits would make the control plane an image CDN.

```text
client ──presigned PUT──► object storage ◄──presigned GET── provider
                               ▲
                               │ authorize, presign, orchestrate, bill
                            this API
```

Both directions are presigned. Writes go through `POST /v1/assets/uploads`, which authorizes the
project and asset type, takes a quota hold on the declared size, chooses a content-addressed key
and returns a presigned PUT; the client transfers; `POST /v1/assets/uploads/{id}/complete` adopts
the object. A `direct_uploads` row (migration `0037`) holds the server's decisions in between, so
the completion call carries only a row id and cannot retarget the upload.

The bucket is also part of the browser contract: it must answer an `OPTIONS` preflight for every
configured `WEB_ORIGINS` value and allow `PUT` plus every header bound into the presign (currently
`content-type` and, when enabled, `x-amz-checksum-sha256`). API CORS cannot grant access to a
different object-storage origin. `scripts/verify_object_storage.py` exercises this preflight before
uploading, so a deployment with valid credentials but unusable browser CORS fails its storage gate.

The presigned PUT binds `x-amz-checksum-sha256`, so the object store rejects bytes that do not
hash to the declared digest — that is what makes a client-declared SHA-256 safe to
content-address a key with, and it is why this service never reads the body to learn the hash.
Size at completion comes from `HEAD`, never from the client. Validation reads a bounded 64 KB
header: magic bytes, declared format, and dimensions for the decompression-bomb bound. The full
decode the multipart path performs is deliberately given up; a truncated file fails at first use,
where `RenditionResolver` already decodes. `POST /v1/assets` remains for deployments with no
object storage, and `POST /v1/assets/uploads` answers `501` there rather than inventing a URL.

Completion has one owner: the `direct_uploads` row is locked, exactly one caller leaves `PENDING`, and the
asset registration, the status change and the quota settlement share one transaction. An authorized upload
whose client never returns is reclaimed by `media_service.maintenance.sweep_expired_uploads`, called both by
`POST /internal/maintenance/expired-uploads` and by the worker loop on `EXPIRED_UPLOAD_SWEEP_INTERVAL_SECONDS`
(default 300; `0` disables). The sweep takes the **same lock order as completion** — the upload row first, its
storage reservation second, both in one transaction, with the `PENDING`/expiry predicate re-read under the
lock — so a sweep and a completion racing for one upload serialise instead of leaving a `PENDING` row beside a
`RELEASED` hold. The object itself is never deleted, and a stale `RESERVED` hold with no `PENDING` upload
behind it is reported for operator reconciliation rather than released: a hold whose registration succeeded
and whose settlement failed must survive.

`StorageProvider.presigned_reference_url()` returns a real presign on S3-compatible storage and
`None` on local disk. `None` is treated as "no fetchable reference" and fails closed before the
submission boundary; it is never a reason to proxy. `LOCAL_REFERENCE_SIGNING_KEY` enables a
signed, expiring, unauthenticated route for local development, which *does* proxy and is
documented as a development affordance rather than a deployment shape.

### Fetching the result back

The outbound half above is presigned. The inbound half — collecting a finished artefact from the
provider — is an SSRF boundary, and `PROVIDER_MEDIA_ALLOWED_HOSTS` is the fence: a provider with no
entry cannot deliver, and the transfer fails closed.

Failing closed is right for a host nobody has confirmed and wrong for a provider the platform
actually routes to, and until 2026-08-27 only `google_flow` was listed. Every OpenRouter, Ark and
DashScope generation therefore reached `COMPLETED` **at the provider — billed —** and then died at
the fetch with `provider media host is not allowlisted`. The failure was at the one point where the
money is already gone.

`openrouter=openrouter.ai,*.openrouter.ai` is now listed, read from the host OpenRouter's own
`/v1/videos/{id}` returns for a finished clip on a real completed job. **Ark and DashScope stay
unlisted deliberately** until a canary shows what theirs are: a guessed host is either a hole in the
fence or another silent failure, and both are worse than a refusal that says why.

The refusal now names the host — `{provider} returned {host!r}` — bounded to 120 characters because
a hostname from a provider response is untrusted input. Without it an operator had to re-run a
billed call to learn a string the provider had already sent.

### Original and derivative

The user's original bytes are immutable. A provider's upload cap is a fact about that provider,
not about the asset — a character's face, a product's label and a fabric's weave only ever exist
at the resolution they arrived at, and re-encoding on the way in destroys the only copy the
project will have.

```text
MediaAsset
├── ORIGINAL             7680x4320  PNG   38 MB   (never re-encoded)
├── PROVIDER_REFERENCE   3840x2160  JPEG   6 MB   (per constraint set, lazy, cached)
└── THUMBNAIL            512px                    (schema only; nothing generates one yet)
```

`GenerationProvider.reference_constraints` declares max pixels, max bytes and accepted formats.
`RenditionResolver` returns the original when it already fits, and otherwise derives a copy
stored as a `media_renditions` row keyed by a digest of those constraints — so a provider that
lowers its limits gets a new rendition instead of silently reusing one built for the old ones.
Derivation compresses before it downscales, refuses below 256x256 rather than shipping a
reference that no longer carries identity, and under a byte cap re-encodes a lossless original
to a lossy format on purpose: a 2048px face with mild compression carries identity a pristine
400px face does not. Constraints that declare no bounds mean limits nobody has established, so
the original is sent unchanged rather than guessed at. Video is reported as unadaptable rather
than transcoded.

Mixing the two modes would submit a reference the provider cannot resolve, so a `FETCHABLE_URL` provider is
never asked to upload and never receives a local asset ID or provider media ID. When no absolute `http(s)`
URL exists — or when live mode would require the provider to fetch a non-HTTPS URL — resolution fails closed
with `PROVIDER_REFERENCE_URL_UNAVAILABLE` before the submission boundary, and the credit reservation is
released. Both modes rewrite the same identifiers inside the flattened Adapter payload, and the resolved
reference fields are protected from Adapter-payload overrides alongside the canonical routing and billing
fields.

The Adapter payload is only valid for the exact target, prompt and reference list it was compiled from. An
automatic retry that switches model or provider, applies a repair patch, or strengthens references recompiles
the payload for the actual target; when it cannot be recompiled — no capability profile, or no canonical shot
spec — the payload is dropped and the canonical request fields alone reach the provider. Plan admission
applies the same rule: re-routing a FREE workspace to another model discards a payload compiled for the
router's original choice.

Logical model names are translated to runtime model IDs by one mechanism shared across providers:
`FLOW_VIDEO_MODEL_KEYS` for Google Flow and `WAN_VIDEO_MODEL_KEYS` for Wan, both fail-closed. Wan resolves the
model family *and* the mode (`t2v`/`i2v`/`r2v`) together, so a version cannot be silently swapped by a
mode-scoped setting. `tests/test_model_routing_integrity.py` enforces that every role binding resolves to a
registered model that declares the role's capability, that no PRIMARY binding targets a transportless stub or a
disabled model, that no enabled model carries a configuration placeholder, that each role has exactly one
PRIMARY, and that fallbacks rank after their primary.

Google Flow maps a selected model to a runtime video model key through an explicitly reviewed table. An
`abra_*` value is an explicit runtime key and passes through; every other ID must be declared in
`FLOW_VIDEO_MODEL_KEYS` (`model=runtime_key`, with `{duration}` expanded to the requested seconds). Only the
legacy `veo` alias ships with a reviewed key, so `flow-veo-3.1` is rejected with `FLOW_MODEL_KEY_NOT_MAPPED`
until an operator declares its key; an undeclared model no longer degrades silently to `abra_t2v_{duration}s`.
The Flow image path is bounded the same way: `imageModelName` comes from a reviewed set rather than an
implicit `NARWHAL` default, so an unset or unreviewed image model fails closed instead of rendering as a
model nobody selected. OpenRouter's Image API follows the same rule through a reviewed *execution envelope*
per model rather than a model-key table, because what must not be guessed there is the model's limits rather
than its ID.

Wan 2.7 is three DashScope models behind one adapter, and its request body is the provider's, not a
normalisation of it. `input` carries `prompt`, `negative_prompt` and — on T2V only — `audio_url`; `parameters`
carries `resolution`, the conditional `ratio`, `duration`, `seed`, `prompt_extend` and `watermark` and nothing
else. I2V and R2V carry their non-text inputs in `input.media`, where each entry's `type` **is** the semantic
role verbatim — `first_frame`, `last_frame`, `first_clip`, `driving_audio`, `reference_image`,
`reference_video` — with an optional `reference_voice` audio URL nested on a reference entry. Nothing is
inferred from array position. The adapter holds each mode to the roles it accepts, to I2V's closed list of
material combinations, to the published reference bounds, and to a duration range that depends on the request
(2–15 seconds, or 2–10 for R2V carrying a reference video). `test_model_routing_integrity.py` pins every one of
those against the registry profile, because two copies of one published limit is how a shot ends up refused in
one place and billed in the other.

Provider request bodies are built from explicit allowlists rather than by dropping underscore-prefixed keys.
OpenRouter video, RunAPI image/video and Ark image forward only their documented transport fields; tenancy,
routing, accounting, idempotency, style embeddings, canonical shot spec and other internal audit metadata
never leave the platform. Each fetchable-URL adapter also reads the Gateway-resolved `start_frame_url`,
`end_frame_url` and `reference_urls` directly, so a Passenger request without an Adapter payload keeps its
references instead of silently dropping them.

### Image generation

`openai/gpt-image-2` on OpenRouter is the project's image model, bound as the `IMAGE_GENERATION` role's
PRIMARY with `seedream-5.0-ark` and `flow-narwhal-image-internal` as fallbacks. `POST /v1/images/generations`
resolves that role rather than naming a model, so the choice lives in the registry and nowhere else; an
explicit `provider` and `model` in the request still override it.

The API is `POST /images` and it is **synchronous**: it answers with the finished images as base64 in the
response body. There is no remote job to poll and no URL to download, which neither end of the submit-poll
Gateway matched. Three additions reconcile it without a second completion path:

| Addition | Purpose |
| --- | --- |
| `ProviderSubmission.result` | A provider whose generation call is synchronous returns its terminal `ProviderJob` with the submission |
| `ProviderJob.outputs` | Inline artefacts (`ProviderInlineOutput`: bytes plus MIME type) in place of `output_url` |
| `MediaRegistry.register_provider_bytes()` | Stores inline bytes through the same content validation a downloaded artefact passes |

The confirmation transaction skips the poll delay when a result is already in hand, then claims the poll and
runs the existing completion path, so billing evidence, credit settlement, candidate and shot status,
idempotency and canary settlement are not duplicated. Batch images beyond the first are registered as project
media bound to the shot but not to the candidate — the workspace paid for them, and a candidate may own only
one artefact.

The result is held in the Gateway process between confirmation and poll, both inside one `process()` call.
Process death in that window loses the artefact but not the accounting: `get_job` for an image reports
`OPENROUTER_IMAGE_RESULT_NOT_RETRIEVABLE` with `submitted=True`, so the credit moves to
`RECONCILIATION_REQUIRED` rather than being silently refunded or reported as success.

Each image model carries a reviewed execution envelope, recorded from its own OpenRouter capability
descriptor: for `openai/gpt-image-2`, 10 images per request, 16 reference images, the published aspect-ratio,
quality and background enums, and a 400K context. A request outside the envelope is rejected locally, before
the paid call; a model with no reviewed envelope is rejected outright. Reference images become
`input_references` entries, and the Gateway-resolved `start_frame_url`, `end_frame_url` and `reference_urls`
are read directly so a Passenger request without an Adapter payload still performs an edit rather than
silently degrading to text-to-image.

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

**A vector carries the space it belongs to.** `EmbeddingSpaceIdentity` — provider, model, model revision,
input schema version, dimension, normalization, distance metric — is stored with every `StyleEmbedding` and
compared before any similarity is taken. It has to be, because the failure mode is silence: cosine over two
vectors from unrelated spaces returns a plausible number rather than an error. A mismatch refuses the lock
when reusing a stored reference, and produces `REVIEW_REQUIRED` with
`STYLE_EMBEDDING_SPACE_CHANGED:<fields>` / `STYLE_SEMANTIC_EMBEDDING_SPACE_CHANGED:<fields>` and **no score**
when evaluating a candidate. Layer 2's space is read after the model answers, because which model answers is
decided per call. What this cannot see is a provider swapping a model behind a stable id at unchanged
dimensions, since no provider wired here publishes a revision.

**Layer 2 fails closed.** With `FEATURE_SEMANTIC_STYLE_LOCK` on, a lock whose semantic reference cannot be
produced is refused (`SemanticStyleLayerRequired`) and nothing is written — `503` when the model was
unreachable, `409` when the reference media could not be read. The lock is append-only and a trigger forbids
re-locking, so a degraded lock would be permanent and would look identical to one made deliberately with the
feature off. With the feature off, a single-layer lock remains the intended outcome and records
`SEMANTIC_EMBEDDER_NOT_CONFIGURED`: fail-closed applies to the layer being enabled, not to its absence.

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

### Two layers, and why one is not enough

The 64-D descriptor is a histogram of colour, tonal, saturation, edge and spatial statistics. It
is a reliable detector for what it was built for — a grade shift, a contrast collapse, a palette
walking away across an episode — and it is deterministic, free and offline, which is why it
stays. It is also blind in a specific way: rendering *medium* barely moves those statistics. Oil
paint and a 3D render of the same scene under the same palette score near 1.0, as do 35mm and a
phone camera. A series can drift from illustrated to photographic with every frame passing.

`ModelRole.STYLE_SEMANTIC_EMBEDDING` (`google/gemini-embedding-2` through the existing OpenRouter
credential) supplies the second layer, which sees medium, brushwork and photographic language and
is correspondingly weak where the descriptor is strong — a regrade that preserves the medium
reads as "same style" to it. Neither subsumes the other.

Migration `0036` binds a second reference embedding to the lock itself, so the two layers can
never describe different frames, and records each layer's verdict separately on
`CandidateStyleEvaluation`. The combined status is the **worse** of the two, never an average:
one layer's confidence must not cover the other's objection. An unavailable semantic model, or a
lock that carries a semantic reference evaluated by a process without an embedder, yields
`REVIEW_REQUIRED` — a missing second opinion is not a passing one, and a gate cannot quietly
weaken itself. A project locked before layer 2 was enabled carries no semantic reference and
keeps the single gate rather than acquiring one retroactively. The layer is a deployment-wide
switch (`FEATURE_SEMANTIC_STYLE_LOCK`, default off), not a per-project flag: it is a paid call
per candidate, and a gate that is quietly stronger on some projects than others is not a gate.

Enforcement lives in `PromptCompilerService`, which resolves the lock from `ProjectStyleService` itself. It used to
live in `prepare_autopilot`, which merged a `style_lock` key into the canonical assets the compiler read — so exactly
one caller produced style-locked prompts and every other caller of `compile()` silently produced prompts with none,
a wrong look rather than an error. A caller-supplied `style_lock` is now only a fallback for a compiler constructed
without a style source; it can never override the authoritative lock, because a prompt compiled against a superseded
style would satisfy every downstream check while rendering the wrong thing. `scripts/simulate_short_story.py` calls
`compile()` plainly and asserts the lock arrives, so the guarantee has a runnable proof.

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
| `RouterObservation` | one generation attempt with its conditions and every observed outcome, for the offline posterior |
| `RouterPosterior` | one saved cell of one offline posterior run, immutable per `run_id` |
| `RouterReplayRun` | one historical replay with its verdict — the evidence the LCB flag's precondition rests on |

`RouterObservation` sits **beside** `ModelMetric`, not in place of it. `ModelMetric` records a metric
name and a value against a provider and a model id, and still drives the adaptive router unchanged;
it cannot say which snapshot ran, what was asked of it, or under what conditions, and each of those
changes what an outcome means. Nothing was migrated between them — the old rows carry no version,
task type or scenario, and inventing them is the contamination the new table exists to prevent, so
`router_observations` starts empty on purpose.

A `RouterReplayRun` row with `passed=false` is as important as one with true: it is the evidence that
`feature_router_lcb` must stay off, and `failure_reasons()` in the report says which of regret,
failure rate, cost or interval calibration failed.

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

Canary status is no longer a static list here. It is a column —
`model_definitions.live_canary_status` — described under "Model pricing, and
refusing to quote what is not priced" below. Known spend is **no longer USD 0**:
Wan 2.7 T2V, I2V and R2V have each completed a real generation, and one
`openai/gpt-image-2` attempt was refused by the provider's router before billing.

### What a permit is worth is what it can still cost

`GLOBAL_CANARY_COST_CEILING_USD` is the only thing between a model-by-model live audit and an
unbounded bill, and until 2026-08-27 it was measuring nothing. The listing endpoint answers
`{"limit": n, "permits": [...]}`; the script read `body.get("items", ...)`, which did not raise — it
fell through to the default and returned an empty list. Every permit ever minted totalled USD 0 of
USD 10, so the ceiling authorised everything while printing a reassuring figure above each canary.

Fixing the key alone would have swapped one wrong answer for another. The rule it was reaching for
charged every permit its full authorisation for ever, including exhausted permits that had billed
nothing — three refused attempts held USD 8.05 of the USD 10 with USD 0 actually spent, which would
have refused the whole remaining audit on money nobody had spent. The rule is now:

```text
ACTIVE               max(authorisation, actual + held)    nothing stops it drawing more
EXHAUSTED/EXPIRED    actual + held                        it can never draw again
```

`held` keeps an unreconciled `UNCERTAIN` usage counted, because UNCERTAIN is not evidence of zero.
`POST /internal/live-canary-usages/{usage_id}/reconcile` is how an operator closes one with a
finding, audited. A listing that fills its page fails closed rather than totalling part of the
history.

## Model pricing, and refusing to quote what is not priced

*Added 2026-08-26. Supersedes the single `estimated_per_second` per model and the
platform-wide resolution multiplier described under "Production evidence and cost".*

### Why the previous shape could not be correct

A billable model carried one number and was scaled by a table shared across every
provider:

```python
{"720p": 1.0, "1080p": 1.30, "2k": 1.65, "4k": 2.4}
```

Twelve billable models used that curve and exactly one had a recorded source. The
curve is not mistuned; no constant exists. Three Veo models from one vendor, priced
by one reseller on one day, have 1080p/720p ratios of **1.0, 1.2 and 1.6**;
Volcengine Ark's Seedance 2.5 puts 1080p at **2.47x** and 480p at **0.44x**, and
480p had no entry at all, so it was billed as though it were 720p.

### `model_pricing_profiles`

One row per **provider × model × input mode × resolution**, effective-dated:

```text
provider  provider_model_id  input_mode  resolution
currency  billing_unit       unit_price          ← how the provider bills
          estimate_unit      estimate_unit_price ← how it lets you plan
usd_per_currency  fx_source  fx_checked_at
estimate_formula  settlement_formula
effective_from    effective_until
source_url        source_checked_at   notes
```

Two prices, not one, because providers bill on quantities nobody can know before
the job exists — Ark settles on `usage.completion_tokens` — and publish a
per-second or per-image figure so a reservation can be taken up front. Keeping
`estimate_formula` and `settlement_formula` separate is what stops a reservation
and a debit from quietly diverging.

Price is stored in the provider's own currency and unit, because that is the only
form in which it can be checked against the published page. The USD conversion
carries its own rate, source and date, so a quote traces back to two dated facts
rather than one rounded number.

**Promotions are dated rows, never edits to a base price.** Ark's limited-time
1080p rate is its own row with `effective_until`, and lapses without anyone acting.
The narrowest price in force wins, promotion before list.

### Fail-closed

`model_definitions.pricing_status` defaults to `UNVERIFIED` and is *derived* from
the profile table by `reconcile_pricing_status()` at boot, so it cannot drift into
a claim nobody recomputes. In live mode an unpriced model raises
`PricingUnverified` instead of quoting a placeholder; outside live mode the
placeholder still serves development and the estimate reports which it used.

`PricingUnverified` subclasses `ValueError` so the routes keep answering 400 — and
because admission catches `ValueError` to price pre-commercial projects at zero, it
is caught explicitly there. Otherwise "we do not know what this costs" would become
"this is free", the one answer that is certainly wrong.

### A logical name is not a provider model ID

The standing rule existed and was violated three times: `seedance-2.5` and
`seedream-5-0` were submitted as API model IDs and name nothing on any provider.
Ark publishes `doubao-seedance-2-5-260628` and `doubao-seedream-5-0-260128`.
`wan-2.7` is *not* an instance of this — it is a family key the adapter resolves
per mode, and a test pins that mapping.

Corrections land in **three** places or they are incomplete: the migration (rows
that exist), `config/model-registry/defaults.json` (fresh deployments), and any
pointer in `config/external-evidence/registry-v1.json`. A migration alone leaves
every new install wrong.

### Cost-affecting parameters are stated, not inherited

A provider default that moves the price is never left unsent. `quality` on
`openai/gpt-image-2` selects across 196–7024 output tokens — a 36x range — and
OpenRouter defaults `generate_audio` to true, billing double the silent rate on
Veo 3.1. Both are now explicit (`OPENROUTER_IMAGE_QUALITY`,
`OPENROUTER_VIDEO_GENERATE_AUDIO`), and a test ties the configured image quality to
the seeded price so the two cannot part company.

### External facts

Live provider facts — model IDs, endpoints, parameters, prices, deprecations — are
researched through the local Grok CLI, **one provider per query**, never mixing IDs
or rates across providers. Every figure is cross-checked against a first-party
source before it reaches code: OpenRouter against its raw SKU JSON, Ark and
DashScope against their published pricing pages and their own worked examples. A
"cannot confirm" is honoured — the model stays `UNVERIFIED` rather than acquiring
an estimate.

### The live canary state machine

`model_definitions.live_canary_status`, with `live_canary_detail` carrying the
reason:

```text
NOT_RUN                no canary attempted
VERIFIED_LIVE          one real generation completed and reconciled
LIVE_BLOCKED_EXTERNAL  attempted, refused outside this codebase
CONTRACT_INVALID       the provider rejected the request we build
```

`LIVE_BLOCKED_EXTERNAL` is the load-bearing one. Contract and pricing can be
audited from a desk; a live canary needs a provider to accept one real request, and
that can be refused by an account setting, a balance or a permission. Without
somewhere to write that down, one blocked provider stalls every model behind it —
and a model that was never proven becomes indistinguishable from one that was,
since both merely lack a success record.

Canary safety: one permit per model at `max_requests=1`, its ceiling sized to the
model's published minimum plus a small margin, under a
`GLOBAL_CANARY_COST_CEILING_USD` measured against what permits *authorise* rather
than what they have drawn. Permit bounds are part of the idempotency key, so
changing a ceiling mints a new permit instead of replaying the old one. **One
attempt per model, ever, and no automatic retry** — a failure is reconciled and
recorded, not repeated.

### One item, one commit

A shared migration that loads rows for several models verifies none of them. Each
model earns its status by completing its own chain — official evidence, contract,
pricing, fresh-deploy seed, recorder proof, tests, no-spend quote — and is then
committed and pushed on its own. Batch the research; never batch the verdict.

### `pricing_status` has to describe the row the till will read

`reconcile_pricing_status()` runs **last** in container startup, after the block that rewrites
`provider_model_id` for every model an operator has declared. Deriving it before those writes
describes a row that no longer exists, and it went wrong in both directions at once: Wan's row moved
to the ID `WAN2_7_T2V_MODEL_ID` names while its price stayed keyed on the family key, so it reported
VERIFIED and then raised `PricingUnverified` at the till — on a fresh install the one model with real
verified generations behind it could not be quoted. Doubao moved off `CONFIGURE_DOUBAO_MODEL_ID` onto
a priced ID and reported UNVERIFIED. The report and the till must read the same row.

## Data architecture and migrations

**One schema authority.** Alembic creates and alters every database. `Database.create_all()` no longer runs at
startup: `build_container()` compares the stamped revision to `REQUIRED_SCHEMA_REVISION` and refuses to start
otherwise, naming `alembic upgrade head`. `create_all_and_stamp()` remains for throwaway databases — a per-test
tmp file, a scratch simulation — and runs only under `DEPLOYMENT_ENVIRONMENT=test`; it stamps what it builds so
the startup check asks one question everywhere. A test asserts the constant equals the alembic head.

**PostgreSQL is the only supported runtime.** Production refuses a non-PostgreSQL `DATABASE_URL`, because under
pysqlite a `begin_nested()` savepoint does not roll back with its enclosing transaction and seven call sites
depend on that rollback to keep a failed step from being committed. Every production guard is a configuration
guard and they all run before the first connection is opened, so a misconfigured deployment is refused for the
reason it is actually misconfigured.

**The test matrix has two halves.** `pytest` runs the shared `container` fixture on SQLite; `pytest
--database=postgres` reroutes it into a throwaway schema in a dedicated `video_platform_test` database, which is
where transaction, savepoint, locking and trigger behaviour is actually answered. Test modules that build their
own `Settings` with a hard-coded SQLite URL are not rerouted. Divergences that are defects in the code rather
than the test are listed in `POSTGRES_KNOWN_DIVERGENCES` as strict xfails, so fixing one fails the run until its
entry is removed.

**A synchronous provider's result is durable, not process-local.** A synchronous image API returns the artefact
in the response body — no remote job to re-read, no URL to fetch — while the Gateway is submit-then-poll. The
result is written to `provider_synchronous_results` (+ ordered `provider_synchronous_result_outputs`) in the same
transaction that confirms the submission, so it becomes durable exactly when the workspace becomes liable, and is
deleted in the same transaction that marks the job terminal. Reading never consumes it: delete-on-read would move
the fatal window from "before the poll" to "between the read and the completion commit". Each output carries a
SHA-256 re-checked on read, and a mismatch fails `submitted=True` so a corrupt artefact reaches reconciliation
rather than being published as a paid result. The row is an inbox, not the media plane — the bytes move into the
media plane on completion, through the same content validation a downloaded artefact passes.

**A stale timeline fence is not conclusive on its own.** The fence is evaluated under the Shot's row lock, so
the loser of a race between two requests carrying the same `idempotency_key` reads the Shot only after the
winner has committed, and reads it `QUEUED` — a fence stale against a change its own duplicate caused. The
create path therefore treats a stale fence as conclusive only once the key is known to be unclaimed; a claim
that exists is a claim for the same request (a differing request hash is still an `IdempotencyConflict`), and
the idempotent answer is the competitor's job rather than a 409.

**`0051_token_pricing` prices what was billing blind.** Twelve of the registry's models bill per
token and none carried a rate, so every text, embedding and refinement call was quoted from nothing.
Every figure was read from the provider's own documentation on 2026-08-26 and the migration records
where. Two details are structural rather than incidental: per-token rates do not survive the column —
DeepSeek's cache-hit rate is 0.007 USD/1M and `numeric(18,8)` rounds it to zero, so rates are stored
per-million — and the OpenRouter video SKUs are recorded at **list price** with the endpoint's
current 15% discount noted but *not* applied, because a discount is a fact about today's promotion
and a price is a fact about the SKU. Five models are deliberately left unpriced rather than guessed.

**Evidence tables enforce what analysis cannot repair.** `router_observations` carries a check
constraint refusing a row that failed generation and still holds a quality score or a rating — nothing
was produced, so there is nothing to judge, and a provider outage recorded as a quality problem would
teach the router to avoid a good model permanently. Its unique constraint on `generation_job_id` makes
a duplicated webhook or a retried worker collapse onto the row already there rather than inflating
every count that reads it, including the effective sample size the LCB gate depends on.
`router_posteriors` is immutable per `run_id`: a re-run gets a new one rather than overwriting, so a
decision taken last week can still be explained by the numbers that were on file last week. Its
ordering constraint checks the two quantiles against each other and says nothing about the mean —
a heavily skewed Beta, the shape a cell with a long run of identical outcomes takes, can have a mean
outside its own central interval, and a constraint assuming otherwise rejects correct arithmetic.

**Every plpgsql guard declares its SQLSTATE.** A `RAISE EXCEPTION` with no ERRCODE reports `P0001`, which
SQLAlchemy raises as `ProgrammingError` — while the same guard under SQLite raises `IntegrityError`. Integrity
guards use `23514`; the character-state head fence deliberately uses `40001`, because a stale fence means
re-read and retry rather than invalid data. A test fails if any guard omits its SQLSTATE.

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
| Base USDC payments | `depay_checkout_sessions`, append-only `depay_webhook_deliveries`, `alchemy_webhook_deliveries`, `onchain_payments`, append-only `workspace_credit_ledger_entries`; legacy wallet-binding/intent tables remain for compatibility |

The migration chain is single-head through:

```text
0024_workspace_credit_lifecycle
-> 0025_flow_project_affinity
-> 0026_model_capability_registry
-> 0027_production_evidence_core
-> 0028_persistent_character_state
-> 0029_project_style_lock
-> 0030_alchemy_usdc_credit_ledger
-> 0031_wallet_binding_payment_intents
-> 0032_depay_payment_links
-> 0033_fixed_depay_pro_offer
-> 0034_narrative_ledger
```

PostgreSQL 17.10 + pgvector 0.8.6 was validated on temporary databases for fresh and populated paths, supported
round trips, `vector(16)`, indexes/unique constraints/foreign keys, credit reservation transactions, generation
enqueue transactions and the tagged Phase III head `0027`. Migration `0028` adds database checks/triggers for project,
character, identity, candidate, timeline, validation and commit ownership; immutable history; forbidden identity keys;
commit evidence; and head fencing on both SQLite and PostgreSQL code paths. Dedicated SQLite schema/migration cases and
positive/negative trigger cases on a fresh temporary PostgreSQL 17 instance pass for `0028`. This is development
evidence, not proof that the historical Compose volume or an existing production database has been upgraded. The
ignored `data/platform.db` is not used as production migration evidence and must not be blindly stamped or upgraded.

Migration `0029` adds a one-time, exact-version project style pointer plus immutable
embedding, lock, and candidate evaluation rows. SQLite migration/schema regression is covered. No PostgreSQL 17,
Compose populated-upgrade, real learned style encoder, or Provider style-control canary evidence is claimed for
`0029` yet.

Migration `0033` is the current code head. It permits provider-managed PaymentIntents without a pre-bound payer wallet,
binds each DePay checkout to exactly one intent, and makes FREE→PRO plus the 3,000-credit append atomic. Fresh SQLite
migration, metadata parity and focused API/service regressions pass offline. A disposable PostgreSQL 17 + pgvector
database passed fresh upgrade only through `0032`; PostgreSQL `0033`, populated/rollback/Compose validation and a real
Base USDC transfer have not been executed.

## Internal observability

`GET /internal/production-evidence` is protected by `PLATFORM_API_KEY` and requires an exact `project_id`, with
optional job/shot filters. It returns redacted model executions, Provider jobs, Provider billing evidence,
CostRecords, Flow bindings, QA evidence, decision outcomes, timeline transitions and stale state. Provider
references are fingerprinted; prompt bodies, vectors, raw Provider responses and credentials are not returned.

`POST/GET /internal/live-canary-permits` creates explicitly confirmed, idempotent permits and lists permit/usage
state. These are development/operator APIs, not a redesigned analytics dashboard.

`POST /internal/live-canary-usages/{usage_id}/reconcile` closes an `UNCERTAIN` canary usage with an
operator's finding and audits it. It exists because `UNCERTAIN` is not evidence of zero: an
unreconciled usage stays counted against the global ceiling until a human says what actually
happened, so the only way to release that hold is to record a finding rather than let it lapse.

`GET /internal/models/router-evidence` returns the four evidence layers **as four keys rather than one
merged list**, because merging them is what the structure exists to prevent: per-layer version, record
and eligibility counts, source-type distribution, per-model-per-version coverage, marked conflicts,
records bound to versions nobody confirmed, and the models with too little evidence to say anything
about. Its `lcb` block answers the question an operator actually has — is the lower bound affecting
routing right now, and if not, which of the three fallbacks is in force — and its `exploration` block
states in its own data that nothing is online. `GET /internal/models/external-evidence` continues to
serve the older frozen `registry-v1.json` unchanged.

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
5. save and verify the DePay Base Native USDC link/callback, execute one explicitly authorized low-value real payment,
   then validate Alchemy reorg/reconciliation operations; grant lifecycle/expiry/admin adjustments also remain;
6. decide the fallback policy for model-backed prompt compilation, then enforce typed JSON output and
   fact-lock validation through `ModelRoleRuntime`; the twelve Skill bodies are rewritten and installed, but
   none has yet been executed by a model;
7. supply the reviewed `FLOW_VIDEO_MODEL_KEYS` entry for `flow-veo-3.1` and an HTTPS `PUBLIC_BASE_URL` the
   fetchable-URL providers can actually reach; the retry/admission payload recompilation, the reference-mode
   split, the fail-closed Flow key mapping and the OpenRouter/Ark payload allowlists are implemented and
   covered by Mock payload contract tests, but no live canary has exercised them.

Item 7 above changed materially on 2026-08-27. `PROVIDER_MEDIA_ALLOWED_HOSTS` listed only
`google_flow`, so **every** OpenRouter, Ark and DashScope generation completed at the provider, was
billed, and then failed at the fetch. OpenRouter is now listed from an observed response host; Ark
and DashScope remain unlisted until a canary reveals theirs, and that is the remaining half of the
item rather than an oversight. The four wrong model IDs behind it are corrected and sourced.

Router evidence is deliberately *not* on that list, because it blocks nothing: with
`feature_router_lcb` false and no exploration call site, the routing behaviour of this release is
identical to the one before it. Its own readiness reads:

    Evidence infrastructure   READY     154 records across three isolated layers, with provenance
    Production posterior      NOT READY router_observations is empty; it fills as the platform is used
    LCB                       OFF       precondition is a replay on file that passed; none can exist yet
    Exploration               OFF       no feature flag and no call site

That is the intended state rather than an unfinished one. 154 pieces of public evidence are an
evidence registry, not a production posterior, and the distance between the two is a calibration
bridge that does not exist and traffic that has not happened. Six model×layer research pairs are
still gaps (`docs/OPEN_ISSUES.md` §1.14) and closing them needs Grok balance, which is exhausted —
neither blocks anything above.

No further Provider integration is required for the persistent-state milestone. The remaining visual blocker is a
trusted, calibrated implementation behind the existing reviewer contract, not permission to treat Voyage embeddings
or another retrieval provider as state authority.

Credential values remain outside the repository. The operator explicitly decided that the current Provider keys
do not require rotation. This decision removes rotation as a blocking action but does not authorize committing,
logging, exposing or automatically using those keys.
