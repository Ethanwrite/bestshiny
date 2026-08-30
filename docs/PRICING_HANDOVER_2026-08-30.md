# Pricing and deployment session handover — 2026-08-30

Read this first, then [`DEPLOY_HANDOVER_2026-08-29.md`](DEPLOY_HANDOVER_2026-08-29.md) for
what came before — its §2–§3.5 are annotated in place with what this session resolved —
and [`DEPLOYMENT.md`](DEPLOYMENT.md) for the host.

This session closed the gate that the previous one left open, applied an operator-supplied
price sheet to every billable model, and deployed twice. Everything below was run, not
inferred; where something is unproven it says so.

## 1. Where it stands

| | |
| --- | --- |
| `main` | `7e80d5a` |
| Production | `153.75.95.10`, `7e80d5a` — **in sync**, recorded in `/opt/bestshiny/DEPLOYED_SHA` |
| Migration head | `0062_canonical_list_pricing` |
| Models | 21 of 24 `live_enabled`; 3 unpriced on purpose |
| Live canary | unchanged — 1 model `VERIFIED_LIVE` (`wan-3.0-openrouter`), the rest `NOT_RUN` |
| Provider spend | unchanged — no live generation was run this session |
| Character Evidence | unchanged — deployed on Modal, `SHADOW`, never run on real media |

Merged: [#19](https://github.com/Ethanwrite/bestshiny/pull/19) canonical list pricing ·
[#20](https://github.com/Ethanwrite/bestshiny/pull/20) deployment-doc corrections ·
[#21](https://github.com/Ethanwrite/bestshiny/pull/21) the recovered payment-configuration
note.

## 2. The PostgreSQL gate is closed

It had lapsed across #14–#17, which merged on the SQLite half alone. Run detached, both
halves, before the work and again with it:

```
baseline 9eb2934     PostgreSQL  1235 passed,  7 skipped   exit 0   17m09s
with 0062            SQLite      1238 passed, 12 skipped   exit 0    5m55s
                     PostgreSQL  1243 passed,  7 skipped   exit 0   26m00s
```

`0061` and `0062` have both now been exercised against PostgreSQL. The PostgreSQL half
takes ~26 minutes on this host, longer than the ~17 the older notes record — budget for
that, and run it detached (`nohup … & disown`); the foreground call is SIGKILLed.

## 3. Canonical pricing, and the three rules it encodes

The operator supplied an audited price sheet and three rules. `0062` applies them and
`tests/test_canonical_list_pricing.py` pins them.

1. **A promotion is never canonical.** Ark's Seedance 1080p row at `55.44 CNY` was
   *modelled correctly* — end-dated rather than folded into the base — and was still the
   row quoting money, because the engine prefers the narrower dated rate while it is in
   force. Deleting it is what stops it quoting; `77.00` was already seeded beside it.
   Generalised: **no row anywhere may carry an `effective_until`**, asserted across the
   whole table, so the next promotion someone seeds fails a test rather than a month of
   billing.
2. **The vendor's original list price beats what is charged today.** `openai/gpt-5.6-sol`
   moved from OpenRouter's resold `2.00/10.00` to the `5.00/30.00` launch list — **a
   2.5–3x rise in that model's estimates.** Intended: a list estimate can only be
   generous, a discounted one under-quotes the moment the promotion lapses.
3. **A price stays in the vendor's currency.** `usd_per_currency` is an FX *snapshot*
   with its own source and date; USD is derived at quote and settlement time. The 30 CNY
   rows were deliberately **not** converted — doing so would erase the difference between
   a price published in dollars and one translated on a particular day.

Scopes that had no published rate before, now seeded: Ark video-input
(42.00/42.00/46.00 CNY per 1M tokens — a `CONTINUE_V2V` shot was **refused outright**
before and now quotes), Wan 2.7 video-input across all three deployment snapshots
including r2v, `veo-3.1-fast` at 4K (0.30 USD/s), and seventeen cache-tier and
modality-input rows that no quote can select.

`scripts/seed_token_pricing.py` now composes `0051`'s table with `0062`'s corrections, so
`--audit` compares a database against canonical pricing *now*. Add future corrections to
that composition; never edit an applied migration.

### 3.1 Three models are unpriced on purpose

`google_flow/flow-veo-3.1` and `google_flow/NARWHAL` (Google sells Flow as subscription
credits, no per-call rate) and `wan/wan3.0-video` (invitation-only, no access from this
account). They are refused a paid route rather than given a plausible number, and a test
asserts they stay that way. This is the honest state, not a gap to fill.

## 4. What is narrowed but not closed

**The estimate/bill divergence (previous handover §3.2).** The price sheet identified the
0.85 factor: it is OpenRouter's *published* 15% endpoint discount, and there is no
separate audio SKU for `alibaba/wan-3.0`. So the billable-duration reading reproduces the
bill exactly (5 × 0.05 × 0.85 = 0.2125) and the audio reading reproduces nothing.

That **reframes** the defect rather than solving it. The gap is not the discount — the
estimator is supposed to quote above a discounted bill and does. It is that a 2-second
request appears to have been billed as **five**. If that is a minimum billable duration,
every short clip is under-quoted at any rate, and the fix is a floor in the estimator
rather than a change to any price. `supported_durations` for that model starts at 2, so a
5s minimum is not something OpenRouter publishes. **One observation is not a floor** —
nothing was changed on the strength of n=1. The accumulated request→cost pairs #16 records
remain the way to settle it.

## 5. What deploying taught, and what is now written down

Two deploys ran this session: `4832066 → 980a6f9` (pricing) and `980a6f9 → 7e80d5a`
(docs). #20 records what they exposed; the short version:

- **Production was a release behind every record.** The previous handover said "in sync
  with `main`" at `9eb2934`; the host was on `4832066`. #18 had been merged and never
  deployed. Documentation-only, so nothing misbehaved — which is why it went a day
  unnoticed. The extracted tree carries no `.git`, so nothing on the host said what was
  running; it took hashing files against candidate commits to find out. **`DEPLOYED_SHA`
  now exists** — read it instead of reconstructing.
- **`docker-compose.prod.yml` IS in the `git archive`**, contrary to what `DEPLOYMENT.md`
  claimed. It is tracked and has no `export-ignore`. It has been byte-identical on both
  deploys so nothing broke, but the next host-side hand-edit would be reverted silently
  while the deploy reported success. Back it up before extracting.
- **The deploy is not zero-downtime.** `up -d` recreates the api container; the second
  deploy's window was 34 seconds. On the first, a real session polling a DePay checkout
  hit it. Check traffic before restarting — `docker compose logs --since 10m web`.

## 6. Open, and owned by the operator

- **The payment framework is parked.** The operator stated on 2026-08-30 that it has many
  unresolved issues and is set aside. Report what you observe; do not open it for repair.
  One concrete observation, recorded and deliberately not acted on:
  **`ALCHEMY_WEBHOOK_ID` is empty on production.** It is *our* config, not something
  Alchemy needs, so delivery is unaffected — but the identity check in
  `core/payments/payment_core/alchemy.py` short-circuits (`if self.webhook_id and …`), so
  a correctly signed delivery from *any* webhook is accepted. The signature boundary is
  untouched and unsigned probes still answer 401. `scripts/preflight_live.py` reports the
  field as BLOCKED. **No test covers the empty branch** — it is the one production runs.
- **The SSH password was pasted into a session transcript twice** and should be rotated.
  Key auth (`ssh -B en0 -i ~/.ssh/cloudzy_ed25519`) is what is in use now, so nothing
  depends on the password.
- **The dev database's `wan-2.7-official` row** held the stale `wan-2.7` id and was
  corrected to `wan2.7-t2v-2026-06-12` to match production. `ensure_defaults()` is
  create-only and never rewrites an existing row, so a stored `provider_model_id` is
  treated as a deployment decision — that correction was made only because it was asked
  for.

## 7. Unchanged from the previous handover

None of this moved, and none of it may be claimed as working: Character Evidence has never
run on real media and no signed callback has been observed; the `VERIFIED_LIVE` writer's
`LIVE_BLOCKED_EXTERNAL` and `CONTRACT_INVALID` branches exist only in unit tests; nine of
ten sweep targets are unrun; backup restore is still unrehearsed on this host.
