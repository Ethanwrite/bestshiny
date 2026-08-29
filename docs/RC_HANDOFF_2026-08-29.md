# Release-candidate handover — 2026-08-29

Read this first, then [`../HANDOFF.md`](../HANDOFF.md) and
[`OPEN_ISSUES.md`](OPEN_ISSUES.md). This document covers one thing the others do not:
**why the live canary sweep has not run, and what the next session must not do about it.**

## 1. Where the work lives

| | |
| --- | --- |
| Branch | `claude/rc-predeploy-integration` |
| Worktree | `/Users/a1-6/Desktop/BestShiny/.worktrees/rc-integration` |
| HEAD | `c13a60d` — working tree clean |
| Base | `origin/main` `4f5dd11`; **14 commits ahead, none pushed** |
| Migration head | `0060_flow_remote_owner_index` (single head) |
| Dev database | already at `0060`; the running `api` container serves this branch's code |

The branch integrates three previously separate workstreams — `origin/main` `4f5dd11`,
`claude/creative-director-episodes` `f5f68c6` (PR #9), and the 2026-08-28 Character Evidence
working tree that used to live uncommitted in the main checkout. **The main checkout was never
modified**; its files were snapshotted read-only. Peer worktrees (`batch-atomicity`,
`thumbnail-gc`, `video-ref-adaptation`, `upper-capabilities`) belong to other sessions — leave
them alone.

## 2. Gate state — measured at `c13a60d`, not claimed

```
SQLite       1194 passed, 12 skipped    exit 0
PostgreSQL   1199 passed,  7 skipped    exit 0
Ruff         all checks passed
Mypy         189 source files
alembic      0052 → 0060 → 0052 → 0060 on a throwaway PostgreSQL database
             single head; `alembic check` → no new upgrade operations
Web          vite build ok; npm audit 0 vulnerabilities
git diff --check  clean
```

Re-measure rather than trusting these if you change anything; the numbers in `HANDOFF.md`,
`PRODUCTION_EVIDENCE.md` and `PRODUCTION_READINESS_CHECKLIST.md` were a commit stale once
already and had to be corrected.

## 3. The live canary is blocked, and money cannot unblock it

The operator authorised **USD 10** for a full vendor sweep. **Nothing was spent (USD 0.00).**
Two independent blockers make a sweep produce billed generations with no evidence.

### 3.1 Artifacts cannot be downloaded from this machine

Every provider hostname resolves into the fake-IP proxy range, and connections terminate on a
local listener:

```
openrouter.ai              → 198.18.0.89     is_global = False
ark.cn-beijing.volces.com  → 198.18.0.96     is_global = False
dashscope.aliyuncs.com     → 198.18.0.182    is_global = False
curl https://openrouter.ai/  → HTTP 200, peer = 127.0.0.1
```

Submissions work (traffic is proxied) — which is why past jobs reached `CONFIRMED`. The artifact
download then hits the SSRF fence in `services/media-service/media_service/registry.py`, which
checks resolution (`:240`) and the connected peer (`:255`). Both refuse a non-global address, so
both fire.

This is already proven in the database: `alibaba/wan-3.0` and `x-ai/grok-imagine-video` reached
`CONFIRMED` at OpenRouter on 2026-08-26/27 — an allowlisted host — and still died with
`PROVIDER_MEDIA_SECURITY_ERROR: provider media host resolved to a non-public address`. The vendor
billed; no artifact was ever retrieved. Those credits sit in admin reconciliation.

**Do not "fix" this by relaxing the fence, widening `PROVIDER_MEDIA_ALLOWED_HOSTS`, or skipping
the peer check.** The fence is correct; the environment is what is wrong. Making a canary pass by
weakening an SSRF control is exactly the fake pass the standing rules forbid. The fix is
operator-side: real DNS for provider hosts, or run the sweep from a network without the proxy.

### 3.2 Nothing can record `VERIFIED_LIVE`

`live_canary_status` appears **only** in `migrations/` and
`packages/domain/production_domain/models.py`. No code path writes it — not `scripts/live_canary.py`,
not the API, not admin. Even a flawless closed loop on a clean network leaves all models at
`NOT_RUN`.

This is an open product decision, put to the operator and **not yet answered**:

- add a writer that stamps `VERIFIED_LIVE` only on a fully closed loop (submitted → completed →
  artifact registered → billing reconciled), recording the provider task id and timestamp; **or**
- keep it manual, with the canary printing evidence for a human to stamp.

Do not implement either until the operator chooses. `VERIFIED_LIVE` is this platform's strongest
production claim, and automating it is a judgement call, not a chore.

### 3.3 What the sweep would cost once unblocked

`scripts/live_canary.py` already covers the registry — 10 targets, caps totalling **USD 6.75**
(`grok-imagine-video` 0.15, `wan-3.0` 0.20, `veo-3.1-lite` 0.40, `wan-2.7` 0.40,
`kling-3-standard` 0.70, `veo-3.1-fast` 0.70, `kling-3-pro` 0.90, `veo-3.1` 2.00,
`gpt-image-2` 0.05, `seedance` 1.25). The script's own `GLOBAL_CANARY_COST_CEILING_USD` is 10 with
**1.10 already committed and only 0.90 actually spent to date**, so the sweep fits both the
ceiling and the operator's budget. It bills only with `--confirm-spend`; `--failure-drill` is free.

Known pre-existing blockers that will bite mid-sweep: the OpenRouter account privacy setting
refuses `openai/gpt-image-2` before dispatch (clear it at openrouter.ai/settings/privacy),
`FLOW_VIDEO_MODEL_KEYS` is empty so Google Flow returns `FLOW_MODEL_KEY_NOT_MAPPED`, and
`wan-3.0-official` has no DashScope access.

## 4. Unfinished work, in priority order

1. **Live canary sweep** — blocked by §3.1 (operator) and §3.2 (decision). Everything else is ready.
2. **`VERIFIED_LIVE` writer or manual runbook** — §3.2, awaiting the operator's choice.
3. **Reconcile the two billed-but-unfetched jobs** from 2026-08-26/27 (`alibaba/wan-3.0`,
   `x-ai/grok-imagine-video`). Real vendor spend, credits held, needs an operator decision —
   `POST /v1/generations/{job_id}/reconcile`.
4. **Character Evidence Modal deploy** — `BLOCKED_EXTERNAL` on `api.bestshiny.com` HTTPS
   reachability. The BestShiny half (durable submission lifecycle, ACCEPTED-timeout
   reconciliation) is live code with tests; the Modal half (atomic `Dict.put(skip_if_exists=True)`
   job claim, callback outbox with scheduled redelivery) is **code only, never deployed**.
   `CHARACTER_EVIDENCE_ENABLED=false` in `.env` is the deliberate, documented switch keeping
   production startup fail-closed-but-startable. See `CHARACTER_EVIDENCE_HANDOFF_2026-08-28.md`,
   whose addendum also records why Voyage embeddings are **incompatible** as AppearanceEncoder
   evidence (vector semantics, artifact-SHA provenance contract, boundary rule).
5. **Wan video reference bounds have no canary** (OPEN_ISSUES §2.9 residual). The numbers are
   declared from Alibaba's own API references and exercised offline through the real rendition
   chain; no live call has confirmed them. Status is "instrumentation complete, provider not
   connected" — not "resolved".
6. ~~Cosmetic residual: `verify_pending_assets` takes a `quota` it never uses.~~ **Closed
   2026-08-29 (`84d1e55`)** — the argument and the always-zero `quota_released` counter are gone
   from the verification sweep; releases live only in `reclaim_rejected_assets`, which deletes
   the bytes first and keeps its own count.
7. **Pushed.** The branch is on `origin/claude/rc-predeploy-integration` and open as
   [#12](https://github.com/Ethanwrite/bestshiny/pull/12); PR #9 was closed as superseded, since
   this branch already contains its commits.

## 5. Traps that cost time in this session

- **`alembic check` from a worktree lies.** The venv is an editable install pointing at the *main
  checkout*, so a bare `alembic`/`python` from a worktree imports the wrong `production_domain`
  and reports every table this branch adds as one to drop. Build `PYTHONPATH` from
  `[tool.pytest.ini_options] pythonpath` in `pyproject.toml` first. Confirmed by
  `python -c "import production_domain; print(production_domain.__file__)"`.
- **The PostgreSQL half takes ~14 minutes** and is SIGKILLed as a foreground tool call. Run the
  matrix detached (`nohup … & disown`) writing an `EXIT=` status file, and poll with an
  `until grep -q` loop.
- **Never `pkill -f pytest`** — peer sessions share this repo and this venv.
- **Do not source `.env` for the test suite**; it exports real provider keys and fails ~27
  provider-configuration tests that look like unrelated regressions. Pass `POSTGRES_PASSWORD`
  alone.
- **SQLite cannot add a table-level CHECK without a batch rebuild**, and the rebuild trips this
  schema's own triggers. `0057` and `0058` create those constraints on PostgreSQL only; the
  deliberate exception is registered in `migrations/env.py` so `alembic check` does not read the
  absence as drift.

## 6. What the previous session's audit found

The work carried out under the earlier context window was audited commit by commit and holds up.
It fixed real bugs — a dependency resolver that reported an obligation settled *later* than the
target shot as `ALREADY_SETTLED` (breaking historical regeneration), a GC-versus-revival race now
closed with row locks, a missing branch-writability check on the commit path — and it caught a
genuine fake pass where the canary reported "Closed loop verified" after an `INVALID_REQUEST` with
no output asset.

Four gaps were found and closed: a doc/code contradiction about released quota, a promised
"deletion lifecycle" that did not exist (now `reclaim_rejected_assets`, which deletes the object
**before** releasing the reservation), a test whose name asserted the opposite of its body, and
gate numbers that were a commit stale.
