# Deployment session handover — 2026-08-29

> **Superseded for current state by [`PRICING_HANDOVER_2026-08-30.md`](PRICING_HANDOVER_2026-08-30.md).**
> This document is still the reasoning behind §2–§3.5; each of those sections is annotated
> in place with what the following session resolved. Read it for *why*, and the newer
> document for *where things stand*.

Read this first, then [`DEPLOYMENT.md`](DEPLOYMENT.md) for the host,
[`MODAL_CHARACTER_EVIDENCE.md`](MODAL_CHARACTER_EVIDENCE.md) for the GPU half, and
[`RC_HANDOFF_2026-08-29.md`](RC_HANDOFF_2026-08-29.md) for what came before.

This session took the release candidate from "runs on a laptop" to "serving on
bestshiny.com", and found five defects by running things rather than reading them.
It also **left one standing gate unmet on four merged PRs**, which is §2 and is the
first thing to fix.

> **Followed up 2026-08-29 (later session).** §2 is closed — the PostgreSQL half
> ran green on `9eb2934`. §3.1, §3.3, §3.4 and §3.5 are resolved; §3.2 is
> narrowed to one reading but not closed. Each section says so in place. The
> pricing work landed as migration `0062_canonical_list_pricing`, against an
> audited price sheet the operator supplied, under three rules recorded in
> `tests/test_canonical_list_pricing.py`.

## 1. Where it stands

| | |
| --- | --- |
| `main` | `4832066` |
| Production | `153.75.95.10`, all services healthy — but **`4832066`, not `main`**: #18 was merged and never deployed, and this row said otherwise for a day. Deployed to `980a6f9` on 2026-08-30. See `DEPLOYMENT.md` §6. |
| Migration head | `0061_retire_wan_logical_name_pricing` (now `0062_canonical_list_pricing`) |
| Models | 21 of 24 `live_enabled` |
| Live canary | 1 model `VERIFIED_LIVE` (`wan-3.0-openrouter`), 23 `NOT_RUN` |
| Provider spend | **USD 1.91** of the operator's USD 10 ceiling |
| Character Evidence | deployed on Modal, `SHADOW`, never run on real media |

Merged this session: [#13](https://github.com/Ethanwrite/bestshiny/pull/13) deployment
and the `VERIFIED_LIVE` writer · [#14](https://github.com/Ethanwrite/bestshiny/pull/14)
artefact fetch authentication · [#15](https://github.com/Ethanwrite/bestshiny/pull/15)
the priced resolution · [#16](https://github.com/Ethanwrite/bestshiny/pull/16) billing
request facts · [#17](https://github.com/Ethanwrite/bestshiny/pull/17) price-gated live
enablement.

## 2. The PostgreSQL half has not run since #13 — fix this first

The standing rule is that **both engine halves must be green**. It was honoured once,
before #13:

```
SQLite       1211 passed, 12 skipped    exit 0
PostgreSQL   1216 passed,  7 skipped    exit 0
```

**PRs #14, #15, #16 and #17 were merged on the SQLite half alone.** Each skip was
reasoned and stated in its PR — no model, migration or query changes in #14–#16, and
#17's migration is two bounded `DELETE`s — but four consecutive skips is not the rule,
it is the rule quietly lapsing. `0061` in particular has never been exercised against
PostgreSQL, and the schema exists to be different there.

Run it before anything else, on `main` at `4832066`:

```bash
POSTGRES_PASSWORD=... .venv/bin/python -m pytest -q --database=postgres
```

Detached — it takes ~15 minutes and is SIGKILLed as a foreground tool call. If it is
green, record the numbers here and the gap is closed. If it is not, that is the most
important thing in this document.

**Closed 2026-08-29.** Run detached on `main` at `9eb2934`, before any of the work
below, and again after it:

```
baseline, 9eb2934      SQLite     (not re-run)          PostgreSQL   1235 passed,  7 skipped   exit 0   17m09s
with 0062 and 3.1      SQLite   1238 passed, 12 skipped  PostgreSQL   1243 passed,  7 skipped   exit 0   26m00s
```

`0061` has now been exercised against PostgreSQL, and `0062` was never merged
without it. The gap the four skipped PRs opened is shut; the rule itself only
stays honoured by being run, not by being written down.

## 3. Defects to fix

### 3.1 `blocked_by` reports pricing where it should report disabled

Known, one line, found after #17 was already deployed. In
`reconcile_live_models` the pricing reason is set for any model with a working
transport, so it masks `model is disabled` for a model that is both. On production
`wan-3.0-official` now reads `no pricing profile` when the truth an operator needs is
that they disabled it pending a DashScope invitation — it sends the reader to fix the
wrong thing.

```python
# apps/api/video_platform_api/main.py
if state.enabled and transport and not priced:   # add `state.enabled and`
```

Cosmetic only: `live_enabled` computes identically either way, so nothing is mis-set.

**Fixed 2026-08-29**, exactly as written above.

### 3.2 Estimates are ~2.1x low and the reason is not settled

A 2s 480p `alibaba/wan-3.0` clip estimates USD 0.101 and bills USD 0.2125. The same
ratio held at 1080p (0.404 → 0.85), so it is **not** a resolution effect. Two
explanations fit exactly and nothing stored distinguishes them:

- five billable seconds at 85% of the published SKU, or
- 2.02 seconds with audio billed at the video rate.

**Do not resolve this by adopting either number.** The operator's rule is explicit:
estimate at the vendor's **list** price, never a discount — a list estimate is only
ever generous, a discounted one under-quotes the moment a promotion lapses, and
`model_pricing_profiles` says so in its own schema comment.

The path is the one the operator set out: `usage.cost` stays the ledger's source of
truth, #16 now records the duration, resolution, aspect ratio and `generate_audio`
actually dispatched beside it, and the estimator is calibrated from accumulated
request→cost pairs. **The two readings diverge at longer durations** — a 6s 480p clip
is ~USD 0.255 under the billable-duration reading and ~USD 0.63 under the audio one —
so ordinary production generations will separate them at no extra cost. OpenRouter
returns only `usage: {cost, is_byok}`, verified against the live API, so nothing more
can be learned from the provider directly.

`generate_audio` stays `true` in production. A canary that disables audio stops
exercising the production path.

**Narrowed 2026-08-29, not closed.** The operator's price sheet identifies the
0.85: OpenRouter's Alibaba endpoint carries a published **15% discount**, so
`0.85 x list` is that discount exactly, not a coincidence of arithmetic. The
sheet also shows OpenRouter publishes **no separate audio SKU** for
`alibaba/wan-3.0` — audio is bundled into the per-second rate — so there is no
published rate for the audio reading to bill *at*. Of the two explanations, the
billable-duration one now reproduces the bill exactly (5 x 0.05 x 0.85 = 0.2125)
and the audio one reproduces nothing (2.02 x 0.05 x 2 = 0.202, and 0.1717 once
the discount is applied).

What this leaves is a different question from the one the section opened with.
The gap is not the discount — the estimator is *supposed* to quote above a
discounted bill, and does. It is that a 2-second request appears to have been
billed as **five**. If that is a minimum billable duration, every short clip is
under-quoted at any rate, discounted or not, and the fix is a floor in the
estimator rather than a change to any price. **One observation is not a floor.**
`supported_durations` for this model starts at 2, so a 5s minimum is not
something OpenRouter publishes, and the accumulated request to cost pairs #16
records are still the way to establish it. Nothing has been changed on the
strength of n=1.

### 3.3 Two Flow models are enabled but unpriced

`google_flow / flow-veo-3.1` and `google_flow / NARWHAL` are `enabled` and now
correctly **not** `live_enabled`, because no published per-call rate exists. They open
the moment one is recorded. Until then this is the honest state, not a bug.

**Confirmed 2026-08-29 and deliberately left as it is.** The operator's price
sheet covers every other billable model and does not price these two, because
Google sells Flow as credits inside a subscription and publishes no per-call
rate. `wan / wan3.0-video` is the third in the same position — invitation-only,
and this account has no access. All three stay unpriced rather than being given
a plausible number, and `test_canonical_list_pricing.py` asserts that they do:
a quotable model with an invented price loses money silently, which is the
failure the pricing table exists to end.

### 3.4 An active promotional rate is the one quoting today

Seedance 1080p has `55.44 CNY/token` ending `2026-09-17` and `77.00` open-ended after.
It is modelled correctly — end-dated rather than written in as the base — but under a
list-price rule it is the single row not quoting list. Operator decision.

**Decided and removed 2026-08-29.** The operator's instruction was explicit:
a temporary promotional price is never written into or used as canonical
pricing, and this one is simply deleted rather than re-dated. `77.00` was
already seeded beside it and becomes the answer with nothing written in its
place. `0062` deletes the row by its own identity — the 55.44 rate and the
2026-09-17 end date `0044` wrote — so a row someone had since corrected would
be left alone. The downgrade restores it exactly.

The generalisation is worth more than the row: **a dated row is the promotional
shape**, because the engine prefers the narrower dated rate while it is in force
by design. `test_canonical_list_pricing.py` therefore asserts that *no* row
anywhere carries an `effective_until`, so the next promotion someone seeds fails
a test rather than a month of billing.

### 3.5 Two pricing shapes worth a second look

Neither is obviously wrong, neither has been verified against the vendor page:
Kling v3.0 pro and std carry **720p only**, and `google/veo-3.1` prices **720p and
1080p identically** at 0.40/s.

**Both verified correct 2026-08-29** against the operator's price sheet.

`google/veo-3.1` genuinely has no 720p-specific SKU: OpenRouter publishes one
`duration_seconds_with_audio` at 0.40 that covers 720p and 1080p alike, and a
separate 4K SKU at 0.60. Identical prices at two resolutions is the vendor's
shape, not a copied row. The three Veo variants disagree with each other about
the 1080p/720p ratio — 1.0, 1.2 and 1.6 — which is the standing argument for
having deleted the platform-wide multiplier rather than retuning it.

Kling pro and std really are 720p-only in the registry, and the price does not
vary by resolution anyway: the published SKU table quotes 0.168 (pro) and 0.126
(std) per second with audio at every resolution it lists. So the 720p rows are
right and adding more would restate one price several times.

The sheet did turn up **one genuine gap** while confirming these: `veo-3.1-fast`
has a 4K SKU at 0.30/s with audio that `0047` never carried. `0062` seeds it.
The registry declares 720p/1080p for that model, so the row is unreachable
today — it is there so that widening the declaration is a config change and not
a repricing.

## 4. Unaudited work

Everything here is code that exists and has not been proven end to end. None of it is
claimed as working.

- **Character Evidence has never run on real media.** The Modal app deploys, all five
  pinned models load on a real T4, auth refuses bad tokens and an authenticated
  request returns 202 — but every probe carried a deliberately unreachable
  `video_url`. **No signed callback has ever been observed arriving at BestShiny**, so
  the outbox and its five-minute redelivery schedule are unexercised. A 202 is
  acceptance, never evidence. Stay in `SHADOW`; promotion needs an approved validation
  plan and none exists.
- **The `VERIFIED_LIVE` writer's failure branches have never fired against a provider.**
  `VERIFIED_LIVE` is proven end to end. `LIVE_BLOCKED_EXTERNAL` and `CONTRACT_INVALID`,
  and the rule that weather records nothing, exist only in unit tests.
- **Nine of ten sweep targets are unrun.** Only `wan-3.0` has been driven end to end.
  `scripts/live_canary.py` covers the rest; see §3.2 before spending.
- **Backup restore has never been rehearsed.** The nightly `pg_dump` runs and produces
  valid output — a restore has not been attempted on this host.
- **The pricing-only canary was never built.** The operator proposed splitting canaries
  into a production one (`generate_audio=true`) and a pricing-only one that overrides it
  per request. The adapter already uses `setdefault`, so a request carrying
  `generate_audio` wins — but `/api/passenger/generate` accepts no `provider_payload`,
  so the field cannot be threaded through today.
- **Modal job ids `probe-1`, `probe-2`, `probe-3` are permanently claimed** in
  `bestshiny-character-evidence-jobs`. Job identities are claimed with
  `Dict.put(skip_if_exists=True)` and never released, so those three can never be
  reused. Harmless; surprising if not written down.

## 5. Changes made to the operator's accounts and data

Recorded because they are not visible in any diff.

- **`uu6first@gmail.com` is now `SUPER_ADMIN`**, via the one-time platform-key
  bootstrap. That route now answers `409`; further role changes go through Admin
  Console RBAC.
- **1,000,000 credits granted** to workspace `eb406a2d…`, through the Admin Console API
  so it is audited as `CREDITS_ADJUSTED` with an idempotency key.
- **The workspace was moved to the `PRO` plan.** `FREE` restricts video to seedance
  regardless of balance, which blocked every canary. This was required, not incidental.
- **The OSS bucket's CORS rule was replaced** with the *union* of production and the
  pre-existing development origins. `PutBucketCors` replaces rather than merges;
  applying production alone would have silently broken local development uploads.
- **All of the admin's browser sessions were revoked**, including one that was not the
  short-lived canary session. That was a mistake — the operator had to sign in again.
  Revoke by session id, not by user.
- **The operator's password was pasted into a session transcript** and should be
  rotated. Still outstanding.

## 6. Traps that cost time

- **SSH to the host needs `ssh -B en0`.** The local TUN proxy makes every TCP port
  appear open and then kills port 22, which reads exactly like the server refusing you.
  `scp` needs `-o BindInterface=en0` — its `-B` means batch mode. `curl` needs
  `--interface en0 --noproxy '*'`; `--interface` alone still honours `HTTP_PROXY`.
  Password auth throttles: retry rather than treating the first failure as real.
- **A pinned upstream plus code written against a later release is invisible to tests**,
  because the tests never import the pinned library. The Character Evidence detector
  called three YOLOX APIs that do not exist at the pinned revision, each surfacing one
  deploy at a time because the first exception hides the next. When an upstream is
  pinned, read the pinned source's signature: `raw.githubusercontent.com/<repo>/<sha>/<path>`.
- **`git worktree` plus squash-merge means `-d` refuses a fully merged branch.** `main`
  can be byte-identical and git still will not see it as merged. Check the content, then
  use `-D`.
- **A test that mirrors the logic it tests agrees with itself.** The first version of the
  priced-resolution test reimplemented the gateway's transform and passed against a
  broken gateway. Drive the real path, and confirm the test fails without the fix.

## 7. What deploying resolved on its own

Three blockers the previous handover carried were environmental, and moving off the
development laptop cleared all three without a line of code: provider hostnames resolve
to real global addresses instead of a fake-IP proxy range, so the SSRF fence passes on
the evidence it actually asks for; object storage exists; and `api.bestshiny.com` is
publicly HTTPS-reachable, which is what Character Evidence was `BLOCKED_EXTERNAL` on.

Worth remembering as a diagnostic habit: for anything touching provider hosts or
reference media, ask whether the environment is wrong before the code is.
