# Product Decisions and Requirements Ledger

Snapshot date: 2026-08-22

This file preserves the product decisions expressed across the project conversation and the five engineering
briefs used to build the repository. It is a requirements ledger, not a claim that every item is implemented.
Implementation truth is maintained in [`../CURRENT_ARCHITECTURE.md`](../CURRENT_ARCHITECTURE.md) and
[`DEVELOPMENT_HANDOFF_2026-08-20.md`](DEVELOPMENT_HANDOFF_2026-08-20.md).

## Source briefs

The original source files remain outside the repository in the Codex attachment store. Their hashes are recorded
so a future engineer can verify the exact source if those files are still available.

| Brief | Source path | SHA-256 | Main scope |
| --- | --- | --- | --- |
| AI Video Platform V1 | `/Users/a1-6/.codex/attachments/264c7fb0-9a5c-4692-ace1-5b0050261e2d/pasted-text.txt` | `ced36f5b9ec88ec29446f24ee9c9fcea1398a8ab10239f244c032d935ff8efed` | Source audit, provider abstraction, Flow, scheduler, jobs, browser worker, storage and Docker |
| AI Director Web Platform V1 | `/Users/a1-6/.codex/attachments/534a5db9-2e82-4991-bcd7-bd234a9c47e2/pasted-text.txt` | `e6a45419524e5f645ced089b8d73fbcef94a48a84c6214a9cbf8b402cad6be02` | Full Web product, narrative/state/identity/continuity/policy/QA/cost/agents |
| Passenger + Autopilot Visual Runtime | `/Users/a1-6/.codex/attachments/f13f3b22-d2e2-4a11-b583-7b3fae40536e/pasted-text.txt` | `6264341c74ac2c2b68c5ac9f32cf092c8eb955e7a28d42817244932b5c351695` | Shared runtime, prompt separation, asset registry, memory, evaluation, routing, credits and benchmarks |
| Phase II Production Intelligence Core | `/Users/a1-6/.codex/attachments/3f2d3504-cde4-463f-be95-d3d0b5e1b954/pasted-text.txt` | `82c49659b83334adb51dd0c1fb29f3d5a5915d7c6e7a9ccd0b44f0c3fae6c71d` | Unified model/provider infrastructure and deterministic algorithm moat |
| Phase III Production Evidence Core | `/Users/a1-6/.codex/attachments/980cdcab-9579-4351-8c79-ea4e51bf5513/pasted-text.txt` | `c232a8b400b703aad1d3f36bd937afb86fe2818e0c73e05fc5b6b0f69559553f` | Real-input/evidence schemas, Flow affinity, single capability truth, model runtime, character evidence, billing/outcomes, timeline v3, live canary, auth/quota and production validation |

The attachment paths are machine-local. This ledger contains the durable decisions required to continue even if
the attachment store is unavailable.

## Phase III decisions and resolved conflicts

- Phase III prioritizes production truth over adding Providers, Skills, agents, UI themes or a credit shop.
- The offline algorithm baseline is frozen at commit `0a74d31`, tag `v0.2.0-algorithm-core-offline`.
- The Phase III implementation is committed at `99f9c60`; its offline evidence snapshot is tagged
  `v0.3.0-production-evidence-core-offline`. This is not a production-release claim.
- Persistent `ModelCapabilityProfile` is the only authoritative model capability/quality-prior source. Wan is 2.7.
- Current external chat/embedding/refinement callers use `ModelRoleRuntime`; deterministic algorithms remain local
  when no model call is needed. Narrative Memory may degrade to structured SQL timeline.
- Actual Provider cost is nullable and can never be synthesized from an estimate. Accepted-shot economics include
  failed and repair attempts, and `DecisionOutcomeRecord` is durable future learning evidence.
- Live execution requires the existing three environment gates plus a bounded `LiveCanaryPermit`; creating a
  permit does not initiate a call. The Phase III real-canary count is zero and known Provider spend is USD 0.
- The 50-vs-87 starter conflict is resolved by using a 4-second omitted-duration Passenger default (about 44
  credits). An explicitly requested 8-second video remains about 87 credits and fails closed when unaffordable.
- The operator explicitly decided that current Provider keys do not require rotation. Keys still stay outside Git,
  logs, fixtures and documentation and do not implicitly authorize live execution.
- The next product milestone is persistent narrative character state, not another Provider integration. Renderer
  choice must remain replaceable behind the state/evidence/commit system.
- `voyage-multimodal-3.5` is an advisory embedding/retrieval component. It cannot assert identity or state facts,
  approve a state delta, or authorize a production commit.

## Highest-level product decision

This is a commercial Web product, not a request to produce one film for the user. Development should improve the
platform, onboarding, workflows, safety, costs and repeatability. Do not spend time generating sample content
unless it is a controlled fixture or an explicitly approved provider smoke test.

## Production hierarchy

The intended creative hierarchy is:

1. Director
   - owns story direction, hook, visual style, immutable creative facts and final approval;
   - may approve, revise or reject, but must not hide provider/retry tactics inside story decisions.
2. Assistant Director
   - converts approved story into executable scenes and shots;
   - one shot has one dominant visible action and explicit start/end states.
3. Cinematography Director
   - owns framing, angle, subject position, gaze target, lighting and one dominant camera movement;
   - never changes story action.
4. Execution agents
   - own structured data, continuity checks, formatting, asset matching and repetitive work;
   - do not make major creative decisions.
5. Prompt agent
   - converts approved specifications into model-specific prompts;
   - preserves identity, environment, props, spatial relations and continuity.
6. QC agent
   - compares output with the approved shot and references;
   - returns `PASS`, `REPAIR` or `REGENERATE`.

The runtime should use expensive models for creative reasoning and deterministic/cheaper execution for repetitive
work. This is an optimization policy, not permission to weaken tenancy, trust, QA or canonical-asset gates.

## Global creative invariants

- Latest explicit user instruction has highest priority.
- Never silently change approved story or product facts.
- Preserve character identity and scene continuity.
- The previous shot's end state should match the next shot's start state unless an explicit transition resets it.
- One generation shot equals one dominant visible action. A minor reaction is allowed only when it does not create
  a second independent trajectory.
- Every visible character has an explicit gaze target.
- A character does not look into the camera unless explicitly approved.
- Use one dominant camera movement per shot.
- Reuse approved assets. A requested replacement creates a new version; it does not overwrite history.
- The database is the authoritative timeline state. Vector/LLM memory is auxiliary retrieval.
- Canonical character identity and mutable narrative state are different products and data layers. Ordinary shots
  may evolve injury, contamination, outfit damage, props, location, lighting and emotion, but cannot silently mutate
  face, body proportions, canonical hair or canonical outfit design.
- A model may propose a state delta. Only server policy plus output-bound evidence, followed by candidate commit,
  may create the next authoritative version. A proposed state never becomes future-shot truth merely because it was
  placed in a prompt or returned by a model.

## Persistent narrative character state requirement

The 2026-08-22 product example requires a character to carry a versioned world-state across shots. The intended
sequence is:

```text
Locked Character Identity
-> committed Character State vN
-> candidate-bound Proposed State Delta
-> deterministic State Policy
-> generation using the proposed target
-> visual fact observation or explicit human review
-> candidate commit
-> Character State vN+1 + branch-head CAS
-> next-shot TimelineState constraint
```

Required invariants:

- the initial state is tied to an already committed shot/candidate and an authenticated human confirmation;
- each state version is immutable, fully materialized, hash chained and scoped to project, character and timeline
  branch;
- a delta records exact paths and base values, source provenance, the base/target hashes, input/output TimelineState
  hashes and candidate ownership;
- a proposal can be persisted only in the Candidate `CREATED`/pre-dispatch allocation transaction. The complete
  proposal-set hash must be bound to both Candidate and Generation Job and rechecked at validate/commit, so evidence
  for generated bytes cannot authorize a later-edited proposal;
- identity paths are forbidden both directly and through ancestor replacement; changing identity is a separate
  explicit workflow, not a narrative-state shortcut;
- policy supports persistent equality/existence locks and scene-bounded locks such as "flare remains unlit until
  scene 14"; constraint IDs are unique, and object replacement expands to changed leaf paths before evidence is
  evaluated;
- changed/locked visible facts require output evidence; missing or untrusted evidence does not pass automatically;
- a confident contradiction rejects the transition, while incomplete/advisory evidence requires explicit human
  review;
- state JSON is bounded to 256 KiB, 5,000 nodes, 12 levels and 200 continuity constraints;
- commit is append-only and ordered `Delta -> Validation -> Version -> Commit -> CAS Head -> Timeline/snapshot ->
  future propagation`; stale head, identity, candidate, proposal-set or Timeline hashes reject the whole transaction;
- the committed version and hash are written into authoritative TimelineState and propagated to the next eligible
  shot, so later shots inherit it as the committed chain advances; the original canonical identity and earlier state
  versions remain intact;
- an explicit `branch_key` may select an immutable version from input and fork it into an independent scope v1/head;
  it must not advance the main head or leak unestablished main/historical bindings into the new branch;
- initial baseline creation updates the typed reference in authoritative TimelineState and propagates it without
  appending a second untyped `ShotStateSnapshot` for the already committed Candidate;
- rollback, audit, compare and regeneration must operate through history/version selection, never by editing an old
  state row.

Current implementation truth: migration `0028_persistent_character_state`, `PersistentCharacterStateService`,
candidate generation/QA/commit integration, prompt target injection, state APIs and the Mira 12→13→14 offline
fixture implement this contract. The fixture preserves the torn sleeve, unlit flare and cold dusk lighting while
committing dried blood/flare relocation/tunnel-edge location as v2 and propagating it to shot 14. It also covers
identity mutation, premature flare ignition, Voyage advisory evidence, visual mismatch, proposal freeze/binding,
explicit branch fork and stale-fence failures. The current working-tree gate is `446 passed, 61 warnings in 89.79s`,
with Ruff format/check, Mypy over 122 source files and `git diff --check` passing; dedicated temporary PostgreSQL 17
trigger tests are development evidence for `0028`, not a production upgrade claim.

The example is intentionally normalized at the storage boundary: "short braids with silver highlights" and the
canonical charcoal field-jacket design belong to the locked `CharacterIdentityVersion` signatures. The mutable state
stores the right-eyebrow injury/blood state, left-sleeve damage, flare state/location, platform position, dusk lighting
and continuity constraints. This prevents a legal injury/outfit-damage update from becoming a hidden identity reset.

This is database/service/fixture evidence. A concrete calibrated production `VLM_REVIEWER`, real-user output
validation and live Provider canary remain unverified. Therefore the system must keep absent or unverifiable visual
provenance fail-closed to human review.

## Commercial product requirements

The user explicitly requested:

- registration and login;
- a substantially redesigned, synchronized workbench UI;
- rounded controls, brighter technology-oriented color, mobile support and plain-language labels;
- researched photography, camera-movement, composition, lighting, commercial, character-consistency and prompt
  Skills installed/configured in the project;
- the ability to resubmit a changed character image, scene image, product/prop image or one selected shot;
- immutable versions and explicit choice of the formal/canonical version;
- a real credit algorithm for Free and paid usage;
- product terminology that ordinary creators can understand;
- commercialization and multi-user operation rather than a one-off production tool.

Current status: the platform implements auth/RBAC, HttpOnly cookie + CSRF, password reset/login throttling, a
responsive workbench, prompt correction, asset versions, workspace storage quota, selected-shot regeneration and
server-priced workspace-credit reserve/settle/refund/reconcile across authenticated generation paths. This is still not a complete automatic
asset-regeneration or commercial-wallet system: automatic character-candidate image generation is not connected
to an image provider, and purchases, recurring grants, expiry and administrator adjustments are not implemented.

The earlier phrase "workbench synchronization" is an unresolved product decision. A future specification must say
which states must synchronize (jobs, asset versions, shot revisions, multi-tab or multi-user edits), which database
record is authoritative, and whether updates are polling, server events or WebSocket based. Do not silently choose
a real-time architecture from that phrase alone.

## Passenger Seat and Autopilot

The product has two entry modes but one infrastructure core.

Passenger Seat:

- user-visible image and video generation;
- image-prompt correction and undo;
- logical model choice rather than provider plumbing;
- reference upload;
- transparent duration, resolution and cost;
- promote a result into a project asset version.

Autopilot:

- director-approved story and shot planning;
- asset resolution and canonical references;
- timeline and continuity state;
- capability-aware model routing;
- internal model-specific shot compiler;
- generation, QA, repair/retry/model switching and commit.

Both modes must share Asset Registry, Project Memory, Model Registry, Provider Router, Generation Gateway,
evaluation, cost/credit records, jobs and storage. A second gateway or second media store is forbidden.

## Prompt boundaries

- Image Prompt Corrector is user-visible and must preserve source language, facts, exact product text, identity
  and requested edit scope. It returns original/corrected prompt, preserved constraints, changes and undo data.
- Video Shot Prompt Compiler is internal. It receives an approved canonical shot specification and cannot invent
  story actions, identity, props, gaze or camera moves.
- Model adapters own protocol fields, reference slots, durations, resolutions and model-specific wording.
- Skills own creative/production reasoning. Providers are execution infrastructure.
- Prompt correction never silently replaces the user's original prompt.

The canonical Video Prompt Compiler remains a project-authored deterministic implementation. The product prompt
refinement path can call fact-locked `ModelRoleRuntime` with a safe local fallback, but Phase III made no live call.
The Skill bodies are also project-authored. Research methods and license decisions are
recorded in `docs/skill-research.md`; upstream Skill bodies were not vendored or added as runtime dependencies.

## Original model/team preferences

The following were supplied as product intent. They are not all current runtime facts:

| Intended role or media task | Original preference | Current implementation truth |
| --- | --- | --- |
| Orchestration/router | Claude coordinates routing, regression retrieval, global-problem detection/reproduction and repair dispatch | No Claude orchestration service exists. Deterministic services and local agent workflows currently perform these responsibilities. |
| Pro Director | Claude Opus 5 | Not registered. Current Phase II default maps `DIRECTOR` to GPT-5.6 Sol through OpenRouter. |
| Assistant Director | Claude Sonnet 5 | Logical model/binding exists through OpenRouter; no live execution verified. |
| Cinematography Director | GPT-5.6 | Logical role maps to GPT-5.6 Sol through OpenRouter; no product caller/live proof. |
| Mobile/execution group | DeepSeek 4 Flash + GLM 5.2 | Not implemented as this team topology. A DeepSeek chat adapter exists without a default role binding. |
| Camera operator | GPT-5.6 | No separate camera-operator service. Cinematography Skill and role exist. |
| Prompt agent | Qwen 3.8 plus advertising/beauty/commercial Skills | Qwen is not integrated. A local prompt compiler and `commercial` Skill exist, with researched commercial/beauty guidance; there are no separate runtime Qwen, advertising-template or beauty-image agents. |
| Browser generation plugin | Doubao Seed Evolving | Not integrated. Browser worker currently targets Google Flow. |
| User Q&A | GLM 5.2 + global RAG | Not implemented. |
| Key/reverse/person/scene frames | Images2 | Not integrated. |
| Advertising/commercial images | Midjourney | Not integrated. Commercial Skill exists only as production guidance. |
| Free reasoning/director | Doubao | Ark/Doubao adapter and Free role bindings exist, but product workflows do not execute them yet. |
| Free image generation | Doubao | Not implemented. There is no `IMAGE_*` ModelRole and the Ark image surface is not connected to the Free product route. |
| Free video | Seedance | Public generation entry points now share server-side Admission and fail closed when the configured Free role/deployment/credits are unavailable; no live call is verified. |

The current `DIRECTOR` to GPT-5.6 Sol mapping is configuration only. It has no connected Director product caller
and no live verification. The original instruction to "limit visual model usage" is retained as a cost-control
requirement, but no numeric quota or trigger was supplied; it remains `TBD` and must not be invented by engineering.

The Director Skill and a Director model role are different things. `skills/director/SKILL.md` is local production
guidance containing immutable-fact/editable-variable boundaries, provider-information separation, approval rules
and hook guidance. A runtime `DIRECTOR` binding chooses a reasoning model. Phase II did not modify the Director
Skill or the deterministic Prompt Compiler relative to the MVP tag.

Model names and APIs change. Runtime IDs must remain configuration/database values, not scattered business-code
constants.

## Model observations supplied by the user

These are qualitative hypotheses for capability profiles and prompt guidance. They are not verified benchmark
results:

- Veo 3.1 Quality: strong text-to-video understanding, fine visual interpretation and stable blocking/trajectory;
  extension can be useful.
- Omni Flash: can lock fixed character assets through instructions; weaker continuous motion and camera-path
  understanding; weaker Chinese; requires strict storyboard execution and few trajectories per shot.
- Grok Images/Video: strong Chinese comprehension, Chinese compositing/effects and instruction following; a single
  shot should not contain many independent movement trajectories. The supplied product rule says not to use a
  15-second Chinese-dialogue shot until it is separately validated because of TTS noise and identity collapse.
  Do not over-constrain an ending frame; video has a strong end-of-shot bias toward looking into the camera.
- Wan 2.2/2.7: expressive emotion and natural movement; long compressed narrative can cause identity collapse or
  duplicated/split people.
- Seedance 2.0/2.5: useful for highlight shots; long compressed narrative has similar identity/splitting risk.
- Proposed media routing: Seedance 2.5 for keyframes, Omni Flash for detail frames if Flow is accessible, Veo 3.1
  Quality for instantaneous scenes, Wan 2.7 for ordinary shots.

The original Veo note used image/video wording ambiguously. Preserve it as a capability hypothesis rather than
silently converting it into a verified text-to-image or text-to-video benchmark. The historical design also
considered reverse-proxy access to Google Flow and Grok to reduce cost, with Grok credit limits noted as a risk.
Any future implementation must comply with provider terms and must not bypass authentication, CAPTCHA, risk
controls or access restrictions. The later Phase II policy still prefers an official Grok adapter.

The earlier Visual Runtime brief also carried Kling capability priors and an experimental Wan 3.0 slot. The latest
product preference names Wan 2.7. Both are historical inputs; the current registry/video JSON disagreement must be
resolved explicitly rather than choosing one silently.

These observations belong in versioned model priors and benchmarks. They must not override actual provider schema,
health, production metrics or user approval.

## Unified model/provider policy

The latest Phase II brief requires business code to request `ModelRole`, `GenerationCapability` or
`EmbeddingCapability`, then resolve:

```text
Model Registry
→ Provider Router
→ Provider Adapter
```

Raw provider names should not be a business-domain contract. Passenger may expose a friendly logical model choice,
but tenancy, plan, trust, criticality and budget still remain server-owned.

Frozen target provider policies:

- Google Flow uses the platform Flow Gateway, account scheduler/pool and browser worker. It does not use
  OpenRouter. Automatic sticky project affinity, uniqueness and explicit migration plans are implemented offline;
  no real Flow account/project canary has been run.
- OpenRouter is the intended unified path for GPT, Claude, Kling and Voyage Multimodal roles.
- Seedance, Veo and Grok prefer official adapters.
- Missing credentials yield `NOT_CONFIGURED` without taking down the platform.
- All ordinary tests use mock or recorded transports.
- A real paid call requires the three-part live gate and an explicit, provider-specific approval when required.

## RunAPI policy

RunAPI is a low-trust Edge provider. It may be used only for low-criticality work such as:

- prompt draft refinement/paraphrasing/translation;
- negative-prompt suggestions and style vocabulary;
- metadata/asset captions and search-query rewriting;
- low-value classification;
- non-canonical tests, temporary placeholders and provider smoke jobs. The media-policy identifiers are
  `NON_CANONICAL_TEST_GENERATION`, `TEMPORARY_PLACEHOLDER_ASSET`, and `PROVIDER_INTEGRATION_SMOKE`.

It may never create or decide:

- canonical/important character identity or master references;
- important scene or hero product masters;
- final keyframes, important dialogue/close-up shots or final video candidates;
- identity QA, canonical timeline assets or production commit assets.

RunAPI output is only a draft. The required prompt flow is fact extraction, locked draft refinement, diff,
constraint validation, and fallback to a trusted OpenRouter model when validation fails. Budget records must include
server-owned task ID/role, estimated cost, actual cost and remaining balance. The current prompt-refinement primitive
derives a deterministic task ID and fixed pricing snapshot from server-owned workspace/project/prompt/fact inputs,
passes a typed internal `EdgeTask`, and validates the structured echo, candidate literals/locked spans,
character-count evidence and bounded English/Chinese negation polarity. This is deliberately a deterministic lexical
guard rather than a claim of general semantic entailment. It is connected to the product refinement path with a
safe fallback, but has not been called live.

RunAPI starts with a product-policy budget of approximately USD 10. It requires the independent
`ALLOW_RUNAPI_EDGE_CALLS` gate in addition to the normal three-part live gate. When remaining budget reaches zero,
the provider must stop being routed automatically. Persistent reserve/settle mechanics and concurrency protection
exist; public JSON cannot supply task identity or price. If the Provider does not return an actual charge, the
estimate remains frozen as `UNCERTAIN` instead of being reported as actual cost. External billing reconciliation
can be performed through a platform-key-only, explicit, idempotent internal decision that either settles verified
actual USD or releases a confirmed no-charge reservation. Automated invoice collection/verification and an
operator-facing workflow are still missing. This Provider USD ledger remains separate from workspace credits.

## Provider project affinity and memory boundaries

- A Google Flow project is pinned to a selected account/provider project and reused across shots rather than
  choosing a random account for every shot. A local project has at most one active binding, while a remote Flow
  project ID keeps one permanent local owner even after a binding becomes disabled or failed.
- Migration is allowed only when the binding is unusable or an authorized operator explicitly requests it. The
  audit must record source/destination account and provider project, reason, actor, time, and affected assets/jobs.
- Login, CAPTCHA, provider risk controls and user authorization are never bypassed. Human action must surface as
  `WORKER_NEEDS_USER_ACTION` or an equivalent visible state.
- Voyage Multimodal is intended only for advisory cross-modal retrieval, supporting similarity and evidence-frame
  ranking. Its output must be typed `ADVISORY`; it is not a face-identity verifier or a discrete state observation
  and must never produce an identity verdict, state-fact assertion, delta approval or commit authorization.
- An automatic mutable-state fact requires a successful same-project `VLM_REVIEWER` execution record, an explicit
  `CHARACTER_STATE_FACT_OBSERVATION` purpose and evidence bound to the exact candidate output asset. Missing,
  mismatched, low-confidence, advisory or otherwise unverifiable provenance routes to human review. Voyage is
  excluded from this authority even if a caller attempts to relabel it.
- Wan's OpenAI-compatible and DashScope API surfaces are both retained as intent. User-specific host and secret
  values are environment configuration, not requirements-ledger data. The current helper contains a
  workspace-specific non-secret host default; it must not be interpreted as a universal official endpoint.

## Provider trust and asset criticality

Provider trust levels:

```text
CANONICAL > PRODUCTION > STANDARD > EDGE > TEST_ONLY
```

Asset criticalities:

```text
CANONICAL, HERO, IMPORTANT, STANDARD, EDGE, TEMPORARY
```

Provider trust must meet the request's hard floor. Cost cannot override this rule. Generated provenance must be
re-checked at canonical promotion and production commit so a low-trust result cannot be relabeled as a user upload.

## Free and paid plan intent

- New Free users receive 50 starter credits.
- Free reasoning should use configured Doubao roles.
- Free image generation should use a configured Doubao/Ark image role; this role does not exist yet.
- Free video should use configured Seedance roles.
- Paid plans may use higher-cost OpenRouter, Flow and other production providers according to role and policy.
- A browser payload may not unlock a provider or plan entitlement.
- Charging must be atomic with generation-job creation and idempotent with the generation request.
- A failed pre-submission transaction must not consume credits; uncertain paid submissions require reconciliation,
  not blind refund/retry.
- A commercial wallet eventually needs grants, purchases, reservations, settlement, refunds, expiry, admin
  adjustment and provider invoice reconciliation.

Implemented on 2026-08-21 for generation usage:

- The server quote, Job, `RESERVED` hold, CostRecord and generation idempotency record are created in one
  transaction. Identity is unique per generation Job and request idempotency remains project-scoped.
- Completion settles the exact original reservation. A proven pre-submit terminal failure/cancel refunds it once.
  Provider-accepted, failed, cancelled or otherwise uncertain remote outcomes remain frozen in
  `RECONCILIATION_REQUIRED`.
- Only the platform-key internal endpoint may resolve an uncertain hold. Its request expresses provider evidence
  (`CONFIRM_PROVIDER_ACCEPTED` / `CONFIRM_PROVIDER_NOT_CREATED`), requires explicit confirmation plus an
  idempotency key, rejects extra financial fields, and derives the full amount/workspace/model from server facts.
- A refunded/settled terminal Job cannot be reused for another paid attempt; a new attempt requires a new Job,
  idempotency key, admission decision and reservation. Automatic QA retry follows the same rule.
- Wallet lifecycle events and manual `DecisionRecord` rows are audit facts. Provider USD/invoice costs,
  `CostRecord`, Flow account credits and RunAPI budget remain separate accounting domains.

Resolved starter experience: with the current static estimate (`$0.09/s`, service multiplier `1.20`,
`$0.01/credit`), an omitted Passenger video duration defaults to four seconds and estimates about 44 credits, so a
50-credit starter workspace can reserve one request. An explicit eight-second request still estimates about 87
credits and fails before Job/Provider creation when the balance is insufficient. Purchases, recurring grants,
expiry and administrator adjustments are not part of this milestone.

Keep four accounting concepts separate:

1. workspace credits owned by the user/workspace;
2. generation supplier cost and `CostRecord` analytics;
3. Google Flow provider-account credits/capacity;
4. RunAPI's USD edge-provider budget.

They may reconcile with one another but must never be represented by one ambiguous balance column.

## Hook and R3 learning policy

The Director Skill currently records the decision-aid equation:

```text
H = suspense × w1
  + attention × w2
  + tension × w3
  + emotional_arousal × w4
```

The planned learning loop is:

1. Segment by audience.
2. Wait until that audience has at least 50 completed, final-QC-passed randomized experiment arms.
3. Use normalized features:

   ```text
   X = [suspense, attention, tension, emotional_arousal] / 100
   y = actual R3
   ```

4. Fit ridge regression.
5. Prevent small-sample weight drift:

   ```text
   final_weight = 70% old_weight + 30% fitted_weight
   ```

6. Randomly retain one rejected hook scored from 40 to 50 as a low-score validation arm, so the system can detect
   whether the 50-point cutoff is wrong instead of only confirming itself.

Known research/product limitations that must remain visible:

- platform traffic metrics are a B2B feature and require customer-controlled UI/API ingestion; do not scrape
  consumer platforms;
- current ridge plan lacks confidence intervals, significance tests and cross-validation;
- a winner rule focused primarily on R3 can suppress other product outcomes;
- references reduce but do not eliminate character drift;
- 1,500 impressions is a fixed threshold and has no dynamic power/sample-size calculation.

Papers and hand-authored scores are weak priors, not authoritative quality truth. No numeric values for
`w1...w4` were supplied or learned, and they must not be invented. None of this experiment platform, data schema,
regression fitting or R3 ingestion is implemented. Only the symbolic hook equation appears in the Director Skill.

## Skill inventory and freeze

Current local Skills:

- `director`
- `short-drama`
- `cinematography`
- `composition`
- `camera-movement`
- `lighting`
- `continuity`
- `character-consistency`
- `commercial`
- `image-prompt-corrector`
- `prompt-compiler`
- `model-prompting`

Phase II freezes broad Skill expansion. Continue algorithm, provider, evaluation and commercial safety work before
adding more Skills. Skill text must not claim a provider feature that the live capability registry does not expose.
The GitHub research request was handled by studying public repositories, methods and licenses and then writing
project-local Skills. Upstream Skill bodies were not copied into the repository. This was a licensing and
maintainability decision and should not be represented as literally downloading every upstream Skill.

## Current conflict resolutions

When earlier and later briefs appear to conflict, use these resolutions:

- Passenger's “manual model selection” means a user-friendly logical model/quality option. It does not authorize
  raw provider/model IDs that bypass plan, trust or cost policy.
- Free provider enforcement is server-side even if the UI filters the list.
- SQL timeline state outranks vector memory and model recollection.
- The committed persistent character-state version/head referenced by SQL TimelineState outranks proposed deltas,
  prompt text, VLM suggestions and retrieved memories.
- A reference image is evidence, not a guarantee of identity consistency.
- Canonical identity is immutable to ordinary shot-state deltas; legal narrative evolution must create a validated
  new state version rather than editing the Character or an earlier version.
- A compiled provider payload is not proof that the real provider transport is deployed.
- A ModelRole interface existing is not live proof. Current product model-execution callers are routed through the
  runtime, while deterministic local algorithms intentionally do not invent unnecessary model calls.
- UI styling must not take priority over Phase II correctness, but nonfunctional visible controls must either be
  wired or removed.

## Credential handling decision

Real credentials for Ark/Seedance, Wan/Alibaba, OpenRouter, RunAPI and DeepSeek were pasted into the conversation.
Their values are intentionally absent from this repository and from this ledger. On 2026-08-21 the operator
explicitly decided that these keys do not require rotation; that decision overrides the Phase III draft's blanket
rotation rule. The repository does not independently verify external credential state.

Only these environment variable names may be documented:

- `ARK_API_KEY`, `ARK_BASE_URL`, `DOUBAO_MODEL_ID`, `SEEDANCE_MODEL_ID`
- `WAN_API_KEY`, `WAN_OPENAI_BASE_URL`, `WAN_DASHSCOPE_BASE_URL`, `WAN_CHAT_MODEL_ID`,
  `WAN2_7_T2V_MODEL_ID`, `WAN2_7_I2V_MODEL_ID`, `WAN2_7_R2V_MODEL_ID`
- `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`
- `RUNAPI_API_KEY`, `RUNAPI_BASE_URL`, `RUNAPI_MODEL_ID`
- `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL_ID`

`scripts/configure_provider_secrets.py` is the intended local interactive helper. It must not echo secrets, commit
`.env`, or open live gates automatically.
