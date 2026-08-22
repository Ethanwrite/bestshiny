# Production Readiness Checklist

Snapshot: 2026-08-22

Current verdict: **NOT PRODUCTION-READY**

An item is checked only when the cited evidence actually exists. Implementation or a Mock test is not treated as
a live Provider result. The authoritative evidence narrative is
[`PRODUCTION_EVIDENCE.md`](PRODUCTION_EVIDENCE.md).

## Baseline and security

- [x] Offline algorithm baseline frozen at commit `0a74d31` and tag `v0.2.0-algorithm-core-offline`.
- [x] Phase III implementation committed at `99f9c60`; offline evidence snapshot recorded as
  `v0.3.0-production-evidence-core-offline`.
- [x] Repository, staged diff and practical Git history secret audit recorded without printing secret values.
- [x] Runtime keys remain outside source, fixtures, documentation, logs and committed environment files.
- [ ] Provider keys rotated/revoked with external control-plane evidence. The operator explicitly waived rotation
  for the current keys; this unchecked item records that no independent revocation/rotation proof exists.
- [x] Normal tests and smoke paths force Mock mode and closed live gates.

## Data and deployment

- [x] Phase III full test result recorded: `406 passed, 57 warnings in 71.58s`.
- [x] Current `0029` offline working tree passes `451 passed, 61 warnings in 88.91s`; Ruff format/check,
  Mypy 122 source files, Node syntax, Alembic single-head and `git diff --check` pass.
- [x] Ruff lint, Ruff format (226 files already formatted), Mypy (121 files), Node syntax and `git diff --check` pass.
- [x] PostgreSQL 17.10 + pgvector 0.8.6 fresh, populated and supported round-trip migrations validated through head `0027`.
- [x] PostgreSQL constraints, vector/index creation, credit transactions and generation enqueue transactions
  validated with recorded evidence.
- [x] Docker Desktop 29.5.3 Compose config/build/up/health passes for Web, API, worker, PostgreSQL and MinIO with no crash loop.
- [x] Docker smoke used fake development credentials, supplied no Provider key, made no live call and was shut down without deleting volumes.
- [ ] Backup/restore and rollback runbooks tested.

## Production evidence core

- [x] Flow automatic affinity and sticky account/project reuse implemented offline.
- [x] Database uniqueness prevents two active local Flow bindings and permanently prevents one remote Flow ID from crossing local-project ownership, including disabled/failed history.
- [x] Flow migration is explicit/fail-closed and requires review when context cannot be transferred.
- [x] Flow polling requires local job, account, project and Provider job identity.
- [x] Persistent `ModelCapabilityProfile` is the single model capability/quality-prior source.
- [x] Wan deployment identity is aligned to 2.7; obsolete per-provider video JSON truth sources are removed.
- [x] Core chat/embedding/refinement execution is routed through `ModelRoleRuntime` where model execution is used.
- [x] Narrative Memory embedding uses the runtime and degrades to structured timeline on vector failure.
- [x] `ModelExecutionRecord` and `EmbeddingEvidence` store redacted, hashed provenance.
- [x] `ProviderBillingEvidence` keeps actual, estimated, reconciled and unknown cost sources distinct.
- [x] Accepted-shot economics include failed and repair attempts.
- [x] `DecisionOutcomeRecord` joins decisions, Provider/model, QA, user outcome and cost evidence.
- [x] `TimelineTransition` v3 and downstream stale/recompute planning are implemented.
- [x] Internal redacted production-evidence and live-canary inspection APIs exist behind `PLATFORM_API_KEY`.

## QA evidence

- [x] Local FFmpeg frame sampling runs against a self-generated, non-user MP4 fixture.
- [x] Detection/tracking/face/appearance components have explicit injectable interfaces.
- [x] View-aware reference selection and confidence-weighted identity aggregation are implemented.
- [x] Tracking uncertainty triggers `VLM_REVIEW_REQUIRED`.
- [x] Hair and costume evidence are reported as `UNAVAILABLE` rather than fabricated.
- [ ] Concrete production detector/tracker/face/appearance models are deployed and calibrated on approved data.
- [ ] Real non-fixture identity evidence has been reviewed for accuracy, bias and failure modes.
- [ ] Production VLM review path is deployed for uncertain tracking/evidence.

## Project style continuity

- [x] A project can be locked once to an exact READY Canonical STYLE version only after explicit real-user confirmation.
- [x] Style descriptors are immutable, version-bound and retain algorithm/model/hash/source-media provenance.
- [x] Autopilot inherits the locked version through reference media, prompt/spec metadata and the internal Adapter
  `style_control` contract; later asset-library Canonical changes do not move the project lock.
- [x] Image/video candidate evaluations persist average/minimum/p10 similarity, drift slope, low-score fraction,
  thresholds and sample provenance.
- [x] Candidate Commit independently requires provenance-matching style `PASS` evidence and cannot be bypassed by
  generic human QA approval.
- [ ] Style encoder/thresholds are calibrated on approved real-user works. The current 64-D descriptor is a
  deterministic offline baseline, not a production learned encoder.
- [ ] Provider-native style controls are mapped from current official contracts and validated by bounded canaries;
  the internal payload alone is not evidence of Provider support.
- [ ] Migration `0029_project_style_lock` has passed fresh/populated/round-trip PostgreSQL and Compose validation.

## Commercial safety

- [x] Workspace credits preserve idempotent, transactional Reserve → Generate → Settle / Refund → Reconcile.
- [x] Starter Passenger video defaults to 4 seconds (about 44 credits); explicit 8 seconds remains about 87 and
  fails closed for a new 50-credit workspace.
- [x] HttpOnly/Secure/SameSite session-cookie policy and double-submit CSRF are implemented.
- [x] Durable login throttling and one-use password-reset tokens are implemented.
- [x] Upload validation and atomic workspace storage reserve/settle/release are active.
- [ ] Email verification, MFA, invitation/member administration and device-session controls are production-ready.
- [ ] Purchases, recurring grants, expiry and admin credit adjustments are implemented and reconciled.
- [ ] Production HTTPS, managed secrets, storage retention, monitoring and alerting are configured.

## Live evidence

- [x] A matching durable `LiveCanaryPermit` is required at model and media live-call boundaries.
- [x] Permit expiry, Provider/model match, idempotency, request ceiling and cost ceiling hard-stop offline tests exist.
- [ ] One RunAPI prompt-refinement canary passed. **NOT EXECUTED.**
- [ ] One OpenRouter role canary passed. **NOT EXECUTED.**
- [ ] One Voyage multimodal embedding canary passed. **NOT EXECUTED.**
- [ ] One low-cost Flow affinity/project operation passed. **NOT EXECUTED.**
- [ ] Actual Provider billing/credit evidence has been ingested and reconciled. **NOT EXECUTED.**
- [ ] Single paid video canary passed end to end. **NOT EXECUTED.**

Do not declare a public beta or production release until all applicable unchecked P0 deployment, data, security and
live-evidence items have documented proof.
