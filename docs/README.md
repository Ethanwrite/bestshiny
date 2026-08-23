# AI Director Platform — Documentation Index

Snapshot: 2026-08-22
Release verdict: **NOT PRODUCTION-READY**

The Phase II offline algorithm core is frozen at commit `0a74d31`, tag
`v0.2.0-algorithm-core-offline`. Phase III was implemented at `99f9c60` with the offline
evidence snapshot tag `v0.3.0-production-evidence-core-offline`; that tag is not proof of a
production release. The current unreleased working tree adds migrations
`0028_persistent_character_state` through `0034_narrative_ledger`, a project-level style lock,
the fixed 30 USDC DePay / Base Native USDC shared payment link, and the series narrative
ledger. No new visual-generation Provider was added.

## Read first

1. [HANDOFF.md — current handoff](../HANDOFF.md)
   - Gate state, what changed this session, and the Git facts to read before committing.
   - Supersedes the 2026-08-20 and 2026-08-22 handoffs; both described states the code no
     longer has and have been deleted.
2. [OPEN_ISSUES.md — everything unresolved](OPEN_ISSUES.md)
   - **Section 1 is the list of things only you can do**: the live-call gates,
     `PUBLIC_BASE_URL`, the Flow model key, the Omni Flash transport, key rotation, and two
     product decisions (deterministic fallback, Chinese-to-English scope).
   - Sections 2–4 are known defects, incomplete work and P2 release blockers. No decision
     needed from you.
3. [CURRENT_ARCHITECTURE.md — the architecture](../CURRENT_ARCHITECTURE.md)
   - Current layering, core data flow, Provider safety boundary, tables and validation
     boundaries. Includes the immutable-identity / mutable-narrative-state split, the
     delta/policy/evidence/commit/head-CAS contract, and the series narrative ledger.
4. [Production evidence report](PRODUCTION_EVIDENCE.md)
   - Separates code evidence, offline fixture evidence and real Provider evidence.
   - Records PostgreSQL, Docker, live canary, actual spend and remaining blockers.
5. [Production readiness checklist](PRODUCTION_READINESS_CHECKLIST.md)
   - An item is ticked only when the cited evidence actually exists.
6. [Product requirements ledger](PRODUCT_REQUIREMENTS_LEDGER.md)
   - Hashes of the five original briefs, product goals, model preferences, credit and
     learning policy. A requirements ledger, not a claim of completion.

## Implementation, security and research records

- [Secret audit](security/secret-audit.md)
  - Redacted record of repository, Git and local-path scans. The operator decided the
    Provider keys in place at that time did not need rotation; that does not change the
    "never persisted, never committed, never logged, never live by default" boundary.
    **Keys pasted into chat on 2026-08-22 are a separate case — see OPEN_ISSUES §1.5.**
- [Skill research and licensing record](skill-research.md)
- [Source and dependency audit](source-audit.md)

## Current verification summary

- Historical offline baseline: `348 passed, 39 warnings`, frozen at `0a74d31`.
- Phase III tag, whole repository: `406 passed, 57 warnings in 71.58s`; Mypy over 121 source
  files, Ruff lint, Node syntax and `git diff --check` passed. Warnings are mostly known
  Alembic/SQLite/Starlette deprecations and the SQLAlchemy FK cycle.
- **Current working tree (2026-08-22): `521 passed, 61 warnings`**; Ruff check, Mypy over 131
  source files, Web production build, npm audit, and a single Alembic head
  `0034_narrative_ledger`. The count rose from 473 to 521 with Provider payload/reference
  contracts, the Flow and Wan model-key mappings, the routing-integrity gate, the structural
  gate over all twelve installed Skills, and the narrative-ledger regressions.
- PostgreSQL 17.10 + pgvector 0.8.6: historical fresh/populated runs, `vector(16)`,
  constraints and transactions all passed when the head was `0027_production_evidence_core`.
  `0032_depay_payment_links` later passed a fresh upgrade and `alembic check` on a disposable
  PostgreSQL 17 + pgvector database. The current head `0034_narrative_ledger` still needs
  PostgreSQL verification, and none of this means a production database or an older Compose
  volume has been upgraded.
- Docker Desktop 29.5.3: Compose config/build/up/health, HTTP 200 smoke and in-container
  Alembic head/check passed at head `0027`, using fake development credentials only, with no
  Provider key supplied.
- Live Provider: RunAPI, OpenRouter, Voyage, Flow and the single video shot are all
  **NOT EXECUTED**. Known spend: **USD 0**.
- Persistent narrative character state: the Mira shot 12→13→14 offline transaction fixture
  covers identity isolation, rule locks, visual evidence, version/commit/head CAS,
  propagation, Voyage degradation to human review, mismatch rejection and the stale fence. A
  proposal may be written only inside the Candidate `CREATED` / pre-dispatch allocation
  transaction; the proposal-set hash binds the Candidate and Generation Job and is rechecked
  at validate and commit. An explicit `branch_key` forks an independent scope v1/head from the
  immutable version chosen by the input, without advancing the main head.
- Character-state JSON is bounded to 256 KiB / 5,000 nodes / 12 levels / 200 constraints.
- A STYLE version must be explicitly promoted to Canonical and then locked to the project once
  by a real user. The locked version binds an immutable embedding, enters every shot's
  reference set, prompt and Adapter payload, and blocks a failing Candidate commit on
  similarity and drift evidence. The current 64-D local descriptor is deterministic offline
  evidence, not a production learned encoder and not a Provider capability claim.
- Series narrative ledger (`0034`): facts, per-holder disclosure and setup/payoff obligations
  are append-only. A character may act on a fact only if it was disclosed to them; audience
  knowledge alone never authorises a character.

The main blockers are deploying and calibrating the production visual detection/tracking/
encoding models and a `VLM_REVIEWER` with verifiable provenance, real Provider and billing
canaries, the remaining public authentication and operational controls, and backup/restore.
`voyage-multimodal-3.5` is an `ADVISORY` retrieval and evidence-frame ranking tool only. It is
not an arbiter of identity or state facts and cannot approve a delta or a commit.
