# Session handover — 2026-08-30 (free-tier gates, rebrand, QA hardening, live E2E audit)

**This is the entry point for the next session.** It covers one long session with four
workstreams. Everything here was run, not inferred; where something is unproven it says so.

Companions, read in this order when you need depth:
[`E2E_AUDIT_2026-08-30.md`](E2E_AUDIT_2026-08-30.md) (the live audit and its findings) ·
[`FREE_TIER_QA_HANDOVER_2026-08-30.md`](FREE_TIER_QA_HANDOVER_2026-08-30.md) (the plan
gates and the three QA fixes) · [`PRICING_HANDOVER_2026-08-30.md`](PRICING_HANDOVER_2026-08-30.md)
(the session before this one) · [`DEPLOYMENT.md`](DEPLOYMENT.md) (the host).

---

## 1. Where everything stands

| | |
| --- | --- |
| `main` | `3b978e5` |
| Production | `153.75.95.10` @ `3b978e5` — **in sync**, verified in `/opt/bestshiny/DEPLOYED_SHA` |
| Migration head | `0064_free_tier_defaults` (dev, staging **and** production all at head) |
| Registry config | `phase2-model-infrastructure-v7` |
| Modal | redeployed twice today; id-switch enforcement, `.aio` handler and the outbox-drain fix are live; still `SHADOW` |
| Gates last run | SQLite `1253 passed, 12 skipped` · PostgreSQL `1258 passed, 7 skipped` · both exit 0 · ruff clean · mypy 190 files clean |

Merged this session: [#23](https://github.com/Ethanwrite/bestshiny/pull/23) free-tier gates +
rebrand + QA hardening · [#24](https://github.com/Ethanwrite/bestshiny/pull/24) release record ·
[#25](https://github.com/Ethanwrite/bestshiny/pull/25) E2E-audit fixes ·
[#26](https://github.com/Ethanwrite/bestshiny/pull/26) provider-download rename + audit doc.

**Databases are unified on one chain and isolated in data**: `video_platform` (dev) and the
newly created `video_platform_staging` share the compose PostgreSQL server; production has its
own. Point at staging with
`DATABASE_URL=postgresql+psycopg://video_platform:<pw>@127.0.0.1:5432/video_platform_staging`.

> One operator instruction could not be taken literally: the requested unification head
> `00**_flow_remote_owner_index` is `0060`, which stopped being head when 0061/0062 landed
> before this session. Everything unified on the **actual** head instead; nothing was downgraded.

---

## 2. What landed

### 2.1 FREE plan — real targets and hard gates (#23, migration 0064)
- Chat: `doubao-free-reasoner` repointed from the `CONFIGURE_DOUBAO_MODEL_ID` placeholder to
  **`doubao-seed-2-0-lite-260428`** and enabled (guarded on the placeholder, so an
  operator-configured value is never overwritten). Images: a new FREE `IMAGE_GENERATION`
  binding → **`doubao-seedream-5-0-260128`**. FREE resolution stays fail-closed.
- Public image tiers map server-side in `entitlement_core.admission.IMAGE_MODEL_TIERS`:
  `shiny`→Seedream, `shinier`→NARWHAL, `shiniest`→gpt-image-2. Pro tiers **deny** FREE, never
  substitute. The browser sends a tier name and can never name an image model.
- Usage gates (Settings: `free_plan_max_images=3`, `free_plan_max_director_turns=10`,
  `free_plan_max_prompt_optimizations=5`): images counted from `generation_jobs` at admission
  (idempotent replays exempt, >1 image/request refused); director rounds counted per creative
  session in the service; optimizations on a row-locked `workspace_usage_counters` row, taken
  before the refine and **handed back when it fails** (verified live, twice).
- Interpretation flagged for the operator: no time window was stated, so images and
  optimizations are totals per FREE workspace and rounds are per session. Changing that is a
  Settings edit (or one query change for a rolling window).

### 2.2 UI — BestShiny Director, no provider internals (#23)
"AI Director" → **BestShiny Director** everywhere; the image quality dropdown
(✨ Shiny — Free / ✨✨ Shinier 🔒 / ✨✨✨ Shiniest 🔒) with locked options disabled for FREE;
`MODEL_LABELS`/`friendlyModel()` so no raw model ID, version hash or per-unit rate reaches a
user surface; `CR` → `credits`; payment-protocol jargon removed; QA summaries humanized; the
new-project dialog uses a placeholder instead of pre-seeded text.
**Temperature/top-p exist nowhere in this product** (UI or backend contract) — nothing to
replace, and nothing fake was added.

### 2.3 The three named QA defects (#23, migrations 0063/0064)
- **`maximum_id_switches_for_decision` is enforced**: `resolve_track_selection` counts
  attributable tracks per character; a person re-entering under a new track ID yields
  `TRACKING_UNCERTAIN` / `ID_SWITCH_LIMIT_EXCEEDED` → ABSTAIN regardless of score margin.
- **Callback duplication closed**: `qa_results.producer_run_id` under a unique
  `(candidate_id, producer_run_id)` index (0063, with a `metrics_json` backfill), and
  `validate_candidate` now does the run check, the insert and the metadata append in **one
  transaction under a `FOR UPDATE` lock** on the candidate; a raced duplicate returns the
  winner's row. The JSON lost-update window is gone.
- **`evaluate_promotion` validates before judging**: it takes the full dataset document and
  enforces `validation/dataset.schema.json` in code (authorization record, per-example
  `consent_record_id`, types, enums), refusing with typed failures before any metric is
  computed. An APPROVED plan cannot override it. A test pins the code's field lists against the
  published schema.

### 2.4 E2E audit fixes (#25, #26) — see §3

---

## 3. The live E2E audit (full record: `E2E_AUDIT_2026-08-30.md`)

A real free-user run on production: registered account → director conversation (real Doubao
calls) → brief approval → key visuals → visual bible → beats → compiled storyboard.
**Seven chain-breaking defects, six fixed and re-verified live, one open.**

Fixed and deployed: director turns 500ing on a canary refusal instead of degrading; Ark's
synchronous image API being polled (a billed artefact stranded); missing Seedance *image*
scheduler capacity; the `EdgeTask` control object crashing four providers' chat wire; image
downloads named `.png` for JPEG bytes; and Modal's outbox losing envelopes (including the one
from the **first real T4 inference**, which the logs show ran 2026-08-29 18:35 UTC).

**Final live state — the image chain works end to end for the first time**: 3 key visuals
`COMPLETED` with real `image/jpeg` assets, visual bible v1 `LOCKED`, beats approved, episode
`6a7b623b` compiled with 7 shots. Total real spend **$0.23**, every canary usage settled with
evidence, zero `UNCERTAIN` remaining.

### 3.1 The one open critical — canary permit economics (audit §4.1)
An unquoted live call holds a permit's **entire remaining budget**; chat costs never settle
(Ark reports tokens, `_actual_cost` wants `usage.cost`); `EXHAUSTED` is terminal even after the
hold settles to ~$0. Permits are therefore strictly one-call regardless of `max_requests`, and
`refine_prompt` still 500s on the refusal.

> **A peer session is landing exactly this fix** on branch `claude/canary-permit-economics`
> (built in an isolated worktree; it touches `entitlement_core/canary.py` + `runtime.py`,
> `cost_core` incl. a new `tokens.py`, `gateway.py`, `main.py`, `container.py`, and tests).
> Coordinate before editing those files. I confirmed to them that none of the uncommitted work
> in the shared checkout is mine — my session's work is entirely merged.

### 3.2 Product findings, in priority order
1. **The director cannot take corrections.** The brief merge never overwrites, and list fields
   (`characters`) are entirely opaque to it — an explicit location correction and a full
   wardrobe description changed nothing across two revisions. Fix: user-turn extractions may
   overwrite (per-field provenance), lists merge by key, plus a field-level edit UI.
2. **Bad extraction poisons every downstream prompt.** `setting.location` swallowed a whole
   clause and became the scene anchor prompt, the scene heading and the action lines.
3. **No identity or style lock in the director flow.** Character anchors never become locked
   identity versions; the style plate never locks project style. Face/wardrobe/hair/style drift
   across compiled shots is therefore unguarded (Character Evidence skips
   `NO_CONFIRMED_IDENTITY_REFERENCES`, the frame-anchor planner downgrades, and hair/costume are
   `UNAVAILABLE` in the evidence stack by declaration). Fix: one explicit post-bible lock step.
4. **FREE quota vs the director's appetite** — a brief can imply 6 key visuals against a
   3-image quota; failed visuals are unrecoverable in place (replays return the same dead job).
5. Medium: template storyboards with canned dialogue contradicting the premise; 38s produced
   for a 30s brief; ~20s director turns (the lite model burns a reasoning chain on JSON
   extraction — disable thinking, stream the turn); tokens recorded but never priced
   (`cost_source=UNKNOWN`); compiled shots display `google_flow`, a disabled route, though FREE
   execution correctly re-routes to Seedance.

Optimized 7-shot prompts (the quality bar the pipeline should reach) are in the audit doc §8.

---

## 4. Owed to the operator

1. **Video generation is quoted and awaiting an option choice** (audit §6): A 1 shot/5s/720p
   ≈ **$1.14** · B 3 shots/16s ≈ **$3.67** · C all 7 shots/38s ≈ **$8.72** · C+ at 1080p
   ≈ **$21.21**. Nothing runs until they pick. Two prerequisites at execution time: an admin
   credit grant to the test workspace (50 starter credits cannot cover one shot ≈134 credits),
   and one canary permit per shot under today's semantics.
2. **Credit reconciliation decision**: the FREE test workspace holds 16 credits in
   `RECONCILIATION_REQUIRED` entries from four post-boundary image failures that were platform
   faults, not provider faults. Recommended: refund the user, keep the provider cost in the cost
   records. Balance is 22 credits of 50; the arithmetic is exact.
3. **The payment framework stays parked** per the 2026-08-30 instruction — its copy changed,
   its behaviour did not.

---

## 5. Live test account (for continuing the E2E)

`e2e-free-audit-0830@bestshiny.com` on production, FREE workspace `a4c914b2`, project
`e5b7d5e7`, creative session `49c9e983`, compiled episode `6a7b623b`. On the host:
session token `/root/.e2e-token`, session id `/root/.e2e-sid`, project `/root/.e2e-project`,
episode `/root/.e2e-episode` (all mode 600).

To take another live step, mint single-use permits first — one per model call:

```bash
curl -s -X POST https://api.bestshiny.com/internal/live-canary-permits \
  -H "Authorization: Bearer $PLATFORM_API_KEY" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: <unique>' \
  -d '{"provider":"seedance","model":"doubao-seedream-5-0-260128","max_requests":1,
       "max_cost_usd":0.10,"expires_at":"<+4h ISO8601>","purpose":"<why>",
       "explicit_confirmation":true}'
```

Reconcile every usage afterwards via
`POST /internal/live-canary-usages/{id}/reconcile` with `SETTLE_ACTUAL_COST` (image
`0.032430`, lite chat `~0.000300`) or `CONFIRM_PROVIDER_NOT_CREATED` when the call never
reached the provider. Leaving usages `UNCERTAIN` is what makes the permit ledger untrustworthy.

---

## 6. Gotchas this session paid for

- **Run every git/pytest command from `/Users/a1-6/Desktop/BestShiny/ai-director-platform`**, or
  pass `git -C <repo>`. The shell's cwd resets between calls; a compound deploy command silently
  shipped a 0-byte archive because `git archive` ran outside the repo and the host still
  extracted it. Verify with `gzip -t` on the host before extracting.
- **Long host-side builds must be detached** (`nohup … & echo $? > /tmp/…exit`) and watched with
  a Monitor until-loop; the tool timeout is shorter than the 2-vCPU build.
- **Polling loops with `sleep` inside a compound Bash command get blocked** — use Monitor with an
  until-loop, or `run_in_background`.
- **`git add -A` in this repo catches `.worktrees/`** (another session's checkout). It is now in
  `.gitignore`; stage explicit paths anyway.
- **Docker Desktop's backend can wedge** so every `docker` call hangs: `pkill -9 -f com.docker`,
  remove `~/.docker/run/docker.sock`, relaunch, then wait on `docker info` in a background loop.
- **A 500 on a director turn or a refine is the canary fence first**, not a code bug — check
  `LiveCanaryDenied` in the API log before debugging anything else.
- **Mock transports never serialize**, which is why the `EdgeTask` wire crash survived a green
  suite for so long. Provider-boundary bugs need transport-level tests that actually
  `json.dumps` the body.
- **Production pins `PROVIDER_MEDIA_ALLOWED_HOSTS` in `/opt/bestshiny/.env`**, so a code-side
  allowlist change needs the env line updated during the deploy too.
- **`git reset --hard` is never safe in this shared checkout.** I ran it to restore local
  `main`'s upstream tracking after pushing a docs commit, and it discarded every uncommitted
  modification to *tracked* files that three peer sessions had live in the tree. Untracked new
  files survived; unstaged edits to tracked files were unrecoverable (never staged, so no blobs
  for `git fsck`). Casualties: the payment session's `container.py` DePay wiring
  (`integration_id` / `dynamic_config_private_key`; its `catalog.py` and migration `0065`
  survived) and the router-evidence session's `runtime_routes.py` route registration plus
  `router_evidence_core/__init__.py` exports (their new modules survived). The canary session
  was unharmed — its work lived in an isolated worktree. All affected sessions were notified.
  Use `git branch --set-upstream-to` alone, or work in a worktree; check `git status` for peers'
  edits before any destructive git command.
- The dev api/worker/web images must be rebuilt after a migration (`docker compose build api
  worker web`) or startup fails the schema-revision check.

---

## 7. Suggested order for the next session

1. Land or review the peer's canary-economics fix (§3.1) — it taxes every live call, and
   `refine_prompt`'s degrade belongs with it.
2. Take the operator's video decision (§4.1) and execute the chosen option; it produces the
   platform's first real Character Evidence signed-callback observations.
3. Director-flow product work in §3.2 order — corrections first, then extraction quality, then
   the identity/style lock step, which is what makes character and style consistency real rather
   than aspirational.
4. Reconcile the credit holds (§4.2) once the operator decides.
