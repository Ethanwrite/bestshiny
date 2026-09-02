# Session handover — 2026-09-02 B (the automatic production budget)

**This is the entry point for the next session.** One workstream: the hand-minted
`LiveCanaryPermit` became the fence for a model's *first* live call only, and every call after
it is fenced by a quote-bound spend authorization under a daily platform/provider USD breaker.
Built, gated on both engines, **not deployed, and off by default even when deployed.**
Companions, in the order you will need them:
[`OPEN_ISSUES.md`](OPEN_ISSUES.md) §1.18 (what to set, and the one product decision you may
overturn) · [`DEPLOYMENT.md`](DEPLOYMENT.md) §6 (the next release's env lines and migration) ·
[`../CURRENT_ARCHITECTURE.md`](../CURRENT_ARCHITECTURE.md) "The automatic production budget" (the
mechanism) · [`SESSION_HANDOVER_2026-09-02.md`](SESSION_HANDOVER_2026-09-02.md) (the session
before; its §4 items are addressed or restated in §4 below).

---

## 1. Where everything stands

| | |
| --- | --- |
| Branch | `claude/canary-permit-auto-budget-155706` on `8b4b9eb` (`main`); at the time of writing the work is **staged, not committed** — the commit and the PR are the operator's call |
| Production | `153.75.95.10`, `DEPLOYED_SHA = 0f90f0b`, `.prev = 9109186`, alembic `0068_xunhupay`, `PROVIDER_MODE=live`, `ROUTER_ADMISSION_POLICY` unset (`cold_start`); router probe still answers `video-router-v3 / CHAMPION_TABLE / seedance` — re-verified read-only this session (§7.1 of the previous handover, done) |
| Migration head on the branch | `0069_production_budget` (dev/test/production still at `0068` until deploy) |
| Gates on the branch | see §5 |
| Budget state after deploy | **off** until `PRODUCTION_BUDGET_PLATFORM_USD_PER_DAY` is set — behaviour identical to today's permit rule |

---

## 2. What landed

### 2.1 The two fences, and the line between them
`production_serviceable()` in `core/model-registry/model_registry_core/live_canary.py` is the
one predicate: `enabled`, `live_enabled`, lifecycle not `DISABLED`/`BLOCKED`, and
`live_canary_status = VERIFIED_LIVE`. Below the line the operator's permit fences the call
exactly as before (whole remaining budget held for a media call, token estimate for a role
call). Above it the call runs on its own authorization. `RuntimeModelState` and
`ResolvedModel` now carry `live_canary_status` (and the latter `lifecycle_status`) so both
runtimes can answer the predicate without another query.

### 2.2 The authorization — `core/entitlements/entitlement_core/production_budget.py`
`GenerationSpendAuthorization` (`generation_spend_authorizations`): one per live operation,
`operation_key` unique (`generation:<job_id>` / `model-role:<uuid>`), `generation_job_id`
unique, bound to workspace + project + provider + model, `max_cost_usd` = the server quote,
status `RESERVED → UNCERTAIN → SETTLED | RELEASED`, `fence = PENDING | PRODUCTION | CANARY`,
`settlement_source = VERIFIED_PROVIDER | TOKENS_LIST | ESTIMATED_QUOTE | RECONCILED_MANUAL`.
Created by `GenerationGateway._create_once` **in the same transaction as
`WorkspaceCreditService.reserve_generation`**, so a tripped breaker rolls job and credits back
together and a refused job leaves nothing held. The quote reaches the gateway as a new
`quoted_cost_usd` keyword from every admission call site (`main.py` ×3, `creative_routes.py`,
`runtime_routes.py`, `production_engine.runtime` `submit`/`submit_passenger`/`submit_autopilot`
and the autopilot retry, `director_production.pipeline`); `GenerationRequest.cost_estimate` is
never read for it. A live job with no server quote is refused (`SpendAuthorizationDenied`, 409).

### 2.3 The breaker
`ProductionBudgetLedger` (`production_budget_ledgers`): one row per UTC day per scope —
`PLATFORM/platform` and `PROVIDER/<name>` — with `limit_usd`, `reserved_usd`, `actual_usd`.
Reservation is a conditional `UPDATE … WHERE reserved + actual + amount <= limit`, platform
first then provider (one lock order, no deadlocks), row created on demand under a savepoint.
`ProductionBudgetExceeded` is `503` at job creation, `RETRY_WAIT` / `PRODUCTION_BUDGET_EXCEEDED`
at the boundary for a job that must reserve again after a local failure released it. Ceilings:
`PRODUCTION_BUDGET_PLATFORM_USD_PER_DAY` (0 = off) and `PRODUCTION_BUDGET_PROVIDER_USD_PER_DAY`
(`provider=usd,…`, each capped at the platform's; a provider without one shares the platform's).
Canary-fenced calls reserve the breaker too when the budget is on: the ceiling bounds *all*
live spend, not only the automatic part.

### 2.4 The gateway (`services/generation-gateway/generation_gateway/gateway.py`)
`_reserve_live_generation_canary` / `_release_…_before_boundary` / `_require_…_boundary` /
`_settle_live_generation_canary` became `_…_fence` twins over a `LiveGenerationFence(canary,
authorization)`. Order at the boundary: take every hold, then mark UNCERTAIN — authorization
first (a RELEASED one re-reserves the current window and the breaker may refuse), permit second;
a failure between the two hands back what was just taken. Settlement: the provider figure if the
poll result carries one, else the quote (`ESTIMATED_QUOTE`) — a breaker that waited for figures
most video providers never send would hold every finished job's reservation forever; a figure
above the ceiling is announced as `SPEND_QUOTE_OVERRUN`. After settlement,
`_record_live_generation_canary_verdict` builds the same `CanaryLoop` the script builds
(reached provider, COMPLETED, artifact registered and `storage.stat`-readable at the recorded
size, credits SETTLED for exactly the held amount) and stamps `VERIFIED_LIVE` on a closed loop
(`LIVE_CANARY_VERDICT_RECORDED`). Every `except LiveCanaryDenied` became `except LiveSpendDenied`
(the new base of `LiveCanaryDenied`, `ProductionBudgetExceeded`, `SpendAuthorizationDenied`).

### 2.5 The model-role runtime (`core/entitlements/entitlement_core/runtime.py`)
`LiveRoleFence` mirrors the gateway. A verified chat/embedding model gets a `MODEL_ROLE`
authorization sized by the `TokenCostEngine` estimate and no permit; an unverified one gets the
permit (plus the breaker hold when the call is priced); a permit-fenced call that settles at a
`VERIFIED_PROVIDER` or `TOKENS_LIST` figure stamps `VERIFIED_LIVE` via the new
`record_role_canary_outcome` — a role call closes its loop inside one request, and that is the
whole loop. `refine_prompt` and `CreativeDirectorService._model_patch` degrade on
`LiveSpendDenied`, so a tripped breaker degrades exactly like a missing permit (the 2026-08-30
production 500 cannot come back through the new fence).

### 2.6 Operator surface
`GET /internal/production-budget` (policy + today's windows), `GET /internal/spend-authorizations`
(by job, operation key, workspace, provider, status), `POST /internal/spend-authorizations/{id}/reconcile`
(`SETTLE_ACTUAL_COST` / `RELEASE_NO_REMOTE_CHARGE`, `Idempotency-Key`, `explicit_confirmation`,
audited as `SPEND_AUTHORIZATION_RECONCILED`, replay-safe, conflict on different facts). The admin
model view carries `production_serviceable`. Migration `0069_production_budget` (two tables;
downgrade refuses over recorded authorizations); `REQUIRED_SCHEMA_REVISION` moved with it.

### 2.7 Tests — `tests/test_production_budget.py` (15)
Policy parsing and capping; the serviceability predicate; breaker atomicity across both rows,
replay, settle-below-hold, exactly-at-ceiling; settle-at-quote, release, re-reserve and the
UTC-day rollover (injected clock); audited idempotent reconciliation; and, on an offline live
gateway with a PRO workspace: a verified model submits with no permit anywhere and settles at
the provider figure; an unverified model waits on `LIVE_CANARY_DENIED`, runs under a permit,
closes its loop, is stamped `VERIFIED_LIVE`, and the next job submits with the permit spent; no
provider figure settles at the quote; a tripped breaker refuses creation with no job, no credit
entry and the balance intact; a live job without a quote is refused; a local failure releases and
the retry re-reserves; an ambiguous failure stays UNCERTAIN and the reconcile endpoint closes it;
budget-off creates nothing; the director earns the automatic path and then consumes no permit;
a tripped breaker refuses a director call as a `LiveSpendDenied`.

---

## 3. Decisions taken this session (yours to overturn)

1. **Background model calls are paid by the plan's quota** — not credits, not the generation
   price. Reasoning and the one-place hook for changing it: `OPEN_ISSUES.md` §1.18.
2. **Verification is mechanical and automatic.** A permit-fenced call that closes its loop
   stamps `VERIFIED_LIVE` itself; the operator's explicit act is issuing the permit. The
   doctrine in `live_canary.py` ("earned by the whole loop and by nothing else") is unchanged;
   what changed is that the gateway and the role runtime now apply it, not only the script.
3. **Off by default.** Deploying this changes nothing until a ceiling is set. A wrong-by-default
   budget is exactly the hole the feature closes; a zero default that silently halts production
   after a deploy would be the opposite failure. Neither; the operator opts in.
4. **Settle at the quote when the provider is silent.** Conservative for the platform (the
   quote is list plus margin), and the only way the breaker's reservations ever drain without
   an operator typing figures in.

---

## 4. Owed to the operator / unresolved

1. **Set the ceilings and deploy** (`DEPLOYMENT.md` §6). Until then nothing here is live.
2. Still from the previous handover: the evidence loop (`router_observations` < 20), lifecycle
   promotion (now: issue one permit per launch model, let the first user call close the loop),
   the video-generation option choice and the credit-reconciliation decision from 2026-08-30-B §4,
   and the shared checkout's local `codex/xunhupay-production` branch (another session's).
3. **Residuals, deliberate and documented (§1.18):** the permit still holds its whole budget for a
   media call; negative canary verdicts are still only the script's; an UNCERTAIN authorization
   stops burdening the *next* day's window on its own but waits for a finding itself; the
   Quality-select / `supported_resolutions` mismatch from the previous handover is untouched.

---

## 5. Gates

Run from the worktree root on the main checkout's venv, the long halves detached
(`nohup … & disown`, status file, per the standing rule), each read for its "N passed" count:

| Gate | Result |
| --- | --- |
| SQLite full suite | first run `1 failed, 1376 passed, 12 skipped` — the one failure was `test_batch_candidate_atomicity` patching the renamed `_settle_live_generation_canary`; repointed at `_settle_live_generation_fence`, file re-run `9 passed` |
| PostgreSQL full suite | `1382 passed, 7 skipped`, exit 0 — 20m53s on this host (memory-pressured; the 9–11 min baseline did not hold, the monitor had to be re-armed once) |
| `ruff check .` | clean |
| `python -m mypy` | 197 source files, no issues |
| `alembic heads` (with `PYTHONPATH` built from `[tool.pytest.ini_options] pythonpath`) | single head `0069_production_budget` |

---

## 6. Gotchas this session paid for

- A `grep` you ran twice can print a heading twice; the file has it once. Anchor edits on what
  `sed -n` shows for the *file*, not on a concatenated command output.
- `zsh` eats `echo ===` (equals-expansion) and unquoted `--include=*.py` (glob). Quote both.
- A frozen dataclass that gains a field with a default (`ResolvedModel.live_canary_status`) keeps
  every positional constructor working — but its equality changes, so anything comparing two
  resolutions across a status change (the runtime's boundary revalidation) now sees the change.
  That is the correct behaviour here and worth knowing.
