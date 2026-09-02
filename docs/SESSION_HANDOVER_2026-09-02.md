# Session handover — 2026-09-02 (scene-champion routing, catalogue alignment, cold-start admission, two deploys)

**This is the entry point for the next session.** One long session, four workstreams, three PRs
merged, production deployed twice. Everything here was run, not inferred; where something is
unproven it says so. Companions, in the order you will need them:
[`DEPLOYMENT.md`](DEPLOYMENT.md) §6 (what runs, and the day's detour) ·
[`ROUTER_EVIDENCE.md`](ROUTER_EVIDENCE.md) §8 (the LCB contract and its 2026-09-01 amendment) ·
[`OPEN_ISSUES.md`](OPEN_ISSUES.md) §1.17 (the switch back to strict is yours) ·
[`SESSION_HANDOVER_2026-08-30-B.md`](SESSION_HANDOVER_2026-08-30-B.md) (the session before; its
§4 "owed to the operator" items are still owed).

---

## 1. Where everything stands

| | |
| --- | --- |
| `main` | `22c4b73` (docs) on `0f90f0b` #38 → `2639a89` #37 → `2090992` #36 |
| Production | `153.75.95.10`, `DEPLOYED_SHA = 0f90f0b`, `.prev = 9109186`, deployed 2026-09-02 14:29Z, **in sync with `main` minus the docs commit** |
| Migration head | `0068_xunhupay` — dev, test and production |
| Routing policy in production | `ROUTER_ADMISSION_POLICY` unset → code default `cold_start`; `POST /internal/router/video` answers `200` / `video-router-v3` / `CHAMPION_TABLE` |
| Gates, final merged tree (`main` + #38) | SQLite `1362 passed, 12 skipped` · PostgreSQL `1367 passed, 7 skipped` · ruff clean · mypy 196 files · `alembic heads` single head |
| Branches | `claude/video-model-routing-5f93e5`, `claude/router-cold-start-admission`, `codex/xunhupay-production` deleted on the remote after merge; the shared checkout `/Users/a1-6/Desktop/BestShiny` still has the **local** `codex/xunhupay-production` checked out (another session's — left alone) |

Memory written this session (`~/.claude/projects/-Users-a1-6-Desktop-BestShiny/memory/`):
`scene-champion-routing-merged.md`, `bestshiny-production-deployment.md` (rewritten for the
day), `long-gates-need-a-detached-run.md` (the empty-gate trap).

---

## 2. What landed

### 2.1 Scene-champion video routing — `video-router-v3` (#36, `2090992`)
The operator's design, verbatim: hard-filter deterministically, then select within a manual
scene → champion table, and let evidence adjust *within* those candidates.

- **Hard filter** (`core/model-registry/model_registry_core/router.py`): task type
  (T2V/I2V/R2V/V2V via the same `router_task_type` the posterior uses), duration, resolution,
  aspect, reference count, trust/criticality, per-axis capability flags, an optional
  `ShotRequirements.max_cost_per_second` ceiling (`COST_LIMIT_EXCEEDED` / `COST_UNKNOWN`), and
  the **per-mode facts** a profile declares under `provider_metadata.modes` — duration ceilings,
  accepted media roles (`MODE_ROLE_UNSUPPORTED`), closed material combinations
  (`MODE_COMBINATION_UNSUPPORTED`). Closes OPEN_ISSUES §2.27 and §2.28 with no migration.
  Only Wan 2.7 declares `modes` today.
- **Champion table** `config/model-registry/scene-champions.json` (`scene-champions-v1`): 11
  derivable scenes, each primary + fallback + written rationale. Operator-named picks: `motion`
  → Seedance 2.5 → Kling 3 Pro; `dialogue_lipsync` → Veo 3.1 → Seedance 2.5;
  `first_last_frame` → Wan 2.7 → Kling 3 Pro. Champions that survive the filter rank in table
  order; open scoring is the fallback when a scene has no entry or no champion survives.
  `RouterDecision` carries `scenario`, `selection_basis`, `champion_audit`; candidates carry
  `champion_rank`. Integrity is a gate: `tests/test_scene_champion_config.py`.
- **Demotion** only with ≥20 **scene-scoped** observations on both sides
  (`RoutingEvidence.scene_sample_counts`, filled only by the LCB overlay's posterior cell —
  pooled per-model counts never qualify) and a blended-score gap > 0.05. The LCB/evidence
  contract is untouched; `FEATURE_ROUTER_LCB` is still false and replay-gated.
- **UI/backend alignment**: `GET /v1/image-tiers`, `GET /v1/models?modality=`,
  `GET /v1/generations?project_id=` (both catalogue endpoints take `project_id` so plan locks
  are the project workspace's); paid "Auto" video repointed `VIDEO_FLOW` → `VIDEO_SEEDANCE`
  (Flow is unpriced); Create-page polling progress bar; dropdowns server-driven with locked /
  unavailable options visible; per-project "My creations" gallery; Runway/Flux removed from
  marketing copy; reconciled provider cost kept server-side; every media URL escaped.
- **Auto is an explicit contract**: `GenerationRequest` no longer defaults
  `provider="google_flow", model="veo"`; an empty pair is `is_auto`, resolved by admission's
  one `_resolve_auto_target` on every route (plan-enforced and bypass), refused by the gateway
  (`PROVIDER_NOT_REGISTERED`) if it ever arrives unresolved. The OpenAI-style
  `POST /v1/videos/generations` no longer fills an omitted pair with Flow.
- Reviewed adversarially twice (five lenses on the first commit, two on the final diff);
  fourteen confirmed findings fixed before merge. Details in `HANDOFF.md`'s 2026-09-01 block.

### 2.2 Cold-start routing admission (#37, `2639a89`)
Deploying #36 showed the automatic router had **no routable model in production**: every
video row is `lifecycle_status = CONFIGURED`, and live mode admitted only `LIVE/DEGRADED`
(pre-existing since v2; passenger Auto/named video resolves roles with `require_live=False`
and never noticed). The operator's phase policy — *evidence decides the ranking, not who is
eligible for a first call* — is `ROUTER_ADMISSION_POLICY` (`packages/shared/platform_shared/
config.py`, default `cold_start`): `registry.routable()` admits every enabled, router-enabled
model of a configured provider except `DISABLED`/`BLOCKED`; `strict` restores LIVE-only. One
pure function, `router_requires_live_lifecycle(settings)`, derives the router flag. Nothing
else moved: gates identical under both policies (`tests/test_router_admission_policy.py`),
quote and reservation still precede submission, every live generation still needs a
`LiveCanaryPermit` at the gateway.

### 2.3 XunHuPay checkout (#38, `0f90f0b`) — another session's work, gated and merged here
`codex/xunhupay-production` (payment UI + `0068_xunhupay`) had been deployed to production
**unmerged** by a codex session between the two `main` deploys. Per the operator's decision it
was merged first: PR opened, merged tree gated on both engines, one fix added on the branch —
the payment-ledger append-only triggers were the only metadata-level PostgreSQL triggers
written as plain `CREATE TRIGGER`, so a second `create_all` on one schema raised
`DuplicateObject`; now `CREATE OR REPLACE TRIGGER` like every other such trigger (`3f341a2`,
a `main` bug since #35; production is alembic-built and unaffected).

### 2.4 Two production deploys (§6 of `DEPLOYMENT.md` has the full account)
`2090992` at 09:48Z, then `0f90f0b` at 14:29Z. Both from a `git archive` of the exact commit,
both markers written, image IDs verified per container, `web` force-recreated after `up -d`
skipped it (third time on record). The detour between them — the unmerged branch deployed
over `main`, making `main` undeployable until it merged — was caught only by
`git diff --name-status <DEPLOYED_SHA> <candidate>` before extraction. **That diff is now the
rule.**

---

## 3. Operator decisions on record (do not relitigate without them)

1. Champion picks as named in §2.1; the table is reviewed policy — change only on their word.
2. `cold_start` is the phase policy for the platform's early life; **they** decide when
   "mature" is and set `ROUTER_ADMISSION_POLICY=strict` (OPEN_ISSUES §1.17).
3. "Do not expand features; do not touch the champion table or thresholds after review" —
   the merge-time instruction for #36/#37.
4. Hard capability gates and the per-request billing ceiling (quote + canary permit) must
   never be relaxed by an admission policy — the 2s/480p-billed-as-5s/1080p incident is the
   reason.
5. XunHuPay merges before `main` deploys (chosen over a composite deploy of an unmerged tip).

---

## 4. Owed to the operator / unresolved

1. **The evidence loop can now start.** With `cold_start` the router makes real picks, and
   `VisualProductionRuntime.evaluate_job` fills `router_observations` one row per evaluated
   generation. `scripts/router_posterior_run.py` still exits 2 (< 20 rows). Nothing to do but
   let it fill; then the LCB flag becomes a decision (OPEN_ISSUES §1.15).
2. **Lifecycle promotion.** No video model is `LIVE`; `alibaba/wan-3.0` alone is
   `VERIFIED_LIVE`. Canaries promote; that is spend, so it is theirs.
3. Still owed from 2026-08-30-B §4: the video-generation option choice (A/B/C/C+ quote) and
   the credit-reconciliation decision.
4. The shared checkout's local `codex/xunhupay-production` branch — the other session's to
   delete (`git checkout main && git branch -D codex/xunhupay-production` there).

Residuals, deliberate and documented: only Wan 2.7 declares `modes`; the tier/model
availability report is stricter than mock-mode admission; a dev-bypass Auto video at
HERO/IMPORTANT criticality now refuses (the default role's only binding is STANDARD-trust)
instead of silently running on Flow; the Quality select ignores `supported_resolutions`, so
Kling at 1080p reaches admission as `PricingUnverified` while the catalogue shows it
available (pre-existing, untouched).

---

## 5. Verifying production (copy-paste)

```bash
# reach the host (the default id_ed25519 is refused; never put this in a $VAR under zsh)
ssh -B en0 -i ~/.ssh/cloudzy_ed25519 -o IdentitiesOnly=yes root@153.75.95.10 \
  'cd /opt/bestshiny && cat DEPLOYED_SHA DEPLOYED_SHA.prev && docker compose -f docker-compose.prod.yml ps'

# the router, from the host (PLATFORM_API_KEY lives in /opt/bestshiny/.env)
K=$(grep -E "^PLATFORM_API_KEY=" .env | cut -d= -f2-)
curl -s -X POST http://127.0.0.1:8080/internal/router/video -H "Authorization: Bearer $K" \
  -H "Content-Type: application/json" -d '{"duration":8,"resolution":"720p"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["router_version"], d["scenario"], d["selection_basis"], d["provider"], d["recommended"])'
# expected: video-router-v3 generic CHAMPION_TABLE seedance doubao-seedance-2-5-260628

# before ANY deploy
git diff --name-status $(ssh ... 'cat /opt/bestshiny/DEPLOYED_SHA') <candidate>
```

Public path from a laptop behind the TUN proxy:
`curl -sS -k --interface en0 --noproxy '*' -H 'Host: api.bestshiny.com' https://153.75.95.10/health`.

---

## 6. Gotchas this session paid for

- **A green gate can be an empty gate.** The Bash tool's cwd persists; after
  `cd apps/web && npm run build`, `ruff check .` passed over zero files and the full pytest run
  exited **0** with `no tests ran`. Anchor gate commands with an explicit `cd <root> &&` and
  read the "N passed" count, never the exit code.
- **Don't put `ssh …` in a shell variable under zsh** — `$SSH 'cmd'` is not word-split and
  fails as "no such file", so a detached deploy silently never starts. Use a shell function.
- **`pgrep -f <script>` over ssh matches the remote shell's own command line.** Check for the
  script's log/status file instead.
- **A worktree branched from a pre-squash tip conflicts with `main` even with no new commits
  there.** Rebase the new commit onto the squash (`git rebase --onto origin/main <old-tip>`).
- **pytest resolves packages from the worktree via `pythonpath`, but plain `python` (and the
  `agents/runtime` gap) resolves the shared checkout via the editable `.pth`** — and the shared
  checkout can be switched to another session's branch under you. Print `__file__` when a
  result makes no sense.
- **Morning-green, afternoon-red on PostgreSQL at the same commit** happened once (the
  `create_all` idempotency bug above). Most plausibly a populated `public` schema in
  `video_platform_test` was masking it and was cleaned in between — conftest warns about
  exactly that. Re-run the failing tests at the last green commit before blaming a branch.
- **Review agents die on the account's session limit** and the workflow then reports their
  findings as "refuted" with empty reasons. Read the journal, resume the run, and treat an
  unverified finding as open.
- **`up -d` skips `web`.** Three deploys running. Compare running vs built image IDs, always.

---

## 7. Suggested order for the next session

1. Confirm production is still `0f90f0b` (`DEPLOYED_SHA`) and the router probe still answers
   `CHAMPION_TABLE` — another session may deploy again without telling anyone.
2. Take the operator's video-generation option (2026-08-30-B §4.1); it now produces the first
   real `router_observations` rows under the champion table.
3. Director-flow product work in 2026-08-30-B §3.2 order (corrections, extraction quality,
   identity/style lock) — unchanged and still the biggest product gap.
4. When ≥20 observations exist, run `scripts/router_posterior_run.py` and bring the exit code
   to the operator with the LCB flag decision.
