# AI Director Platform — Production Evidence

Evidence date: 2026-08-21

Scope: Phase III “Production Evidence Core” offline checkpoint

Release verdict: **NOT PRODUCTION-READY**

This report distinguishes implemented evidence plumbing from evidence collected against a real Provider. The
Phase III full-repository gate is `406 passed, 57 warnings in 71.58s`.

## Executive truth

- The previous offline algorithm core is frozen at commit `0a74d31` and tag
  `v0.2.0-algorithm-core-offline`.
- Phase III implementation is committed at `99f9c60`; this evidence snapshot is tagged
  `v0.3.0-production-evidence-core-offline`.
- Ordinary tests, migrations and Docker checks remain offline: `PROVIDER_MODE=mock` and
  `ALLOW_LIVE_PROVIDER_CALLS=false`.
- No real RunAPI, OpenRouter, Voyage, Flow, Seedance, Wan or other Provider call was executed in this phase.
- Known Provider spend caused by this development phase is **USD 0**.
- The single paid video canary is **NOT EXECUTED**.
- Adapter presence, a compiled payload, a permit implementation or a fixture pass is not reported as a live
  Provider verification.

## Evidence matrix

| Area | Implemented evidence | Validation state | Important limit |
| --- | --- | --- | --- |
| Git checkpoints | Phase II commit `0a74d31` / tag `v0.2.0-algorithm-core-offline`; Phase III implementation `99f9c60` / evidence tag `v0.3.0-production-evidence-core-offline` | Both checkpoints are recoverable | The Phase III tag records offline evidence, not production readiness |
| Secret handling | Repository/history/path audit in [`security/secret-audit.md`](security/secret-audit.md); credential status schema | No tracked runtime key found | Operator explicitly decided that current keys do not require rotation; keys remain external and were not independently validated |
| PostgreSQL + pgvector | Migrations `0025`–`0027` add affinity, capability and production-evidence state | PostgreSQL 17.10 + pgvector 0.8.6 fresh/populated/round-trip, constraints, vector and transactions passed through head `0027` | Temporary validation database, not the ignored mixed local SQLite file |
| Docker | Compose declares Web/API/worker/PostgreSQL/MinIO and keeps live gates closed | Docker Desktop 29.5.3 config/build/up/health/HTTP smoke passed with fake development credentials | Local production-like smoke, not public production deployment |
| Flow affinity | Atomic first-use allocator, sticky account/project binding, controlled migration plan and four-part poll identity | Offline database/service regression | No real Flow account/project creation was executed |
| Capability truth | Persisted `ModelCapabilityProfile` is the authoritative model capability/quality-prior source; old video JSON profiles were removed; Wan is aligned to 2.7 | Offline registry/router/admission regression | Provider runtime observations still require merge/validation; no live schema discovery |
| Model calls | `ModelRoleRuntime` executes chat, embeddings and fact-locked refinement and records every attempt | Offline/mock runtime regression | Deterministic narrative/compiler paths remain intentionally local; no paid role call executed |
| Voyage memory | Narrative Memory requests `MULTIMODAL_EMBEDDING` through `ModelRoleRuntime`; failure degrades to structured SQL timeline and records `MEMORY_VECTOR_DEGRADED` | Offline/mock regression | No real Voyage embedding canary was executed |
| Model evidence | `ModelExecutionRecord` stores role/model/provider/request hash/latency/tokens/cost source/status; `EmbeddingEvidence` stores dimension and hashes, never full vectors | Offline persistence regression | Actual cost stays null without trusted Provider evidence |
| Character evidence | `CharacterEvidenceProducer` performs FFmpeg frame sampling, injected detection/tracking, view-aware face and appearance matching, confidence weighting, temporal aggregation and versioned thresholds | Self-generated, non-user MP4 fixture; local deterministic inference doubles | Production detector/tracker/face/appearance models are not bundled or deployment-validated; hair/costume are `UNAVAILABLE` |
| Character QA safety | Low-quality samples have lower weight; uncertain tracking requires semantic/VLM review; evidence records sample/reference/encoder versions | Offline fixture regression | No real-user identity result and no production VLM review occurred |
| Timeline | Relational `TimelineTransition` v3, nine transition types, branch/reconciliation semantics, downstream stale marking and planning-only recompute | Offline SQL regression | Committed media remains immutable; no public committed-shot edit workflow is claimed complete |
| Billing truth | `ProviderBillingEvidence` separates `VERIFIED_PROVIDER`, `ESTIMATED`, `RECONCILED_MANUAL` and `UNKNOWN`; missing Provider amount leaves `actual_cost = null` | Offline gateway/cost regression, including Mock/Recorded cost-spoof rejection | Only LIVE-mode Provider evidence can populate verified actual cost; no external invoice or live billing metadata was collected |
| Accepted-shot economics | All failed, accepted and repair attempts are included in accepted-shot/wasted-cost aggregation and provider/model performance | Offline cost regression | Observations are blended with manual priors (`0.80/0.20`, minimum 20 samples) and do not replace priors at low sample counts |
| Decision dataset | `DecisionOutcomeRecord` joins shot features, continuity, policy, provider/model, candidate, QA, user outcome and cost source | Offline candidate/commit regression | It contains fixture/mock outcomes only in this phase |
| Live canary | Durable permit/usage, provider+model match, expiry, request ceiling, cost ceiling, idempotency, pre-boundary release and uncertain/settled accounting | Offline hard-stop regression | A permit authorizes one bounded operation; it does not itself execute a Provider call |
| Commercial auth | HttpOnly session cookie, Secure in production, SameSite=Lax, double-submit CSRF, durable login throttle and one-use password reset | Offline API regression | Email verification, MFA, invitations and device-session management remain outside this phase |
| Storage quota | Workspace max/used/reserved bytes plus atomic reserve/settle/release and `MediaAsset.size_bytes` | Offline concurrency/API regression | Default quota and plan policy still need operations review |
| Starter credits | Omitted Passenger video duration now defaults to 4 seconds, estimated at about 44 credits; explicit 8 seconds remains about 87 and fails closed against a 50-credit balance | Offline admission/wallet regression | Purchases, recurring grants, expiry and admin adjustments are not implemented |
| Observability | Internal, platform-key-only read API joins model execution, Provider jobs, Flow bindings, QA, decision outcomes, costs and timeline transitions; separate permit create/list API | Offline authorization/redaction regression | No operator dashboard redesign; project/model execution linkage is project-scoped where no job/shot FK exists |

## Flow affinity guarantees

For Google Flow, complementary partial unique indexes enforce both active local affinity and permanent remote
ownership so that:

```text
one local project + google_flow -> at most one active binding
one google_flow remote project  -> one permanent local owner across every binding status
```

`FlowProjectAllocator` reuses the ready binding and its account. A migration is explicit and stateful; it does not
silently round-robin to another account when continuity-bearing Provider context may be lost. A migration plan
records source/target account and project plus characters, instructions and assets; non-transferable state becomes
`USER_REVIEW_REQUIRED`. Polling requires local generation job, Provider account, Provider project and Provider job
identity together.

The default project provisioner fails closed. These controls are implemented and tested offline, but no real Flow
project was provisioned in Phase III.

## Cost and outcome semantics

The workspace wallet remains:

```text
Reserve -> Generate -> Settle / Refund -> Reconcile
```

It is separate from Provider USD/credit evidence. A Provider completion without trusted billing facts creates
`UNKNOWN` evidence, retains the estimate and leaves `actual_cost` null. A trusted Provider-reported amount or
credit delta can create `VERIFIED_PROVIDER` evidence. Manual reconciliation is explicitly labeled
`RECONCILED_MANUAL`.

For a committed shot, accepted-shot cost includes every candidate/repair attempt. Provider performance aggregates
attempts, accepted count, QA/identity/camera/action pass rates, latency, failures, verified total, estimated total,
waste and cost per accepted shot. The router blends observations with human priors and scales the observation
weight by sample coverage so a few calls cannot overwrite reviewed priors.

## Live-provider controls

Normal live execution still requires all three environment gates:

```env
PROVIDER_MODE=live
ALLOW_LIVE_PROVIDER_CALLS=true
LIVE_PROVIDER_CONFIRMATION=I_UNDERSTAND_THIS_COSTS_MONEY
```

RunAPI additionally requires `ALLOW_RUNAPI_EDGE_CALLS=true` and the existing Edge/criticality/budget policy. A
matching, non-expired `LiveCanaryPermit` is required at the final `ModelRoleRuntime` or media-generation boundary.
The internal permit endpoint requires `PLATFORM_API_KEY`, explicit confirmation and an idempotency key; creating a
permit never initiates a call. Request/cost exhaustion is a hard stop.

Actual canary results for this phase:

| Canary | Result | Spend |
| --- | --- | ---: |
| RunAPI prompt refinement | NOT EXECUTED | USD 0 |
| OpenRouter role request | NOT EXECUTED | USD 0 |
| Voyage multimodal embedding | NOT EXECUTED | USD 0 |
| Flow low-cost operation | NOT EXECUTED | USD 0 |
| Single paid video shot | NOT EXECUTED | USD 0 |

## Verification ledger

| Gate | Result |
| --- | --- |
| Historical offline-core freeze | `348 passed, 39 warnings`; Ruff, Mypy, Node and SQLite/Alembic checks passed before commit `0a74d31` |
| Phase III full repository test | `406 passed, 57 warnings in 71.58s`; warnings are mainly known Alembic/SQLite/Starlette deprecations and SQLAlchemy FK-cycle warning |
| Phase III Ruff / Mypy / Node | Ruff lint passed; Ruff format reports 226 files already formatted; Mypy passed over 121 source files; Node syntax and `git diff --check` passed |
| Alembic head | `0027_production_evidence_core` |
| PostgreSQL 17 + pgvector | PostgreSQL 17.10 + pgvector 0.8.6 fresh/populated/round-trip, `vector(16)`, constraints and credit/enqueue transactions passed |
| Docker build/up/health | Docker Desktop 29.5.3: config, three image builds, up, service health, HTTP 200 smoke and in-container migration check passed; fake credentials and no Provider keys |
| Real Provider calls | NOT EXECUTED |

## Remaining production blockers

1. Deploy and calibrate concrete local detection/tracking/face/appearance inference before treating character QA as
   production identity evidence.
2. Execute separately authorized low-cost canaries, then the single video canary, only when a bounded permit exists.
3. Collect real Provider billing/credit evidence and compare it with server estimates.
4. Finish email verification, MFA/invitations/device-session operations, backup/restore, monitoring/alerts and
   production secret/storage/HTTPS deployment.
