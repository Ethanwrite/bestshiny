# Free-tier gates, UI de-jargoning and QA hardening — 2026-08-30

Read this after [`PRICING_HANDOVER_2026-08-30.md`](PRICING_HANDOVER_2026-08-30.md). One
session, four workstreams: the three Character Evidence defects the operator named, the
FREE plan's hard usage gates, the user-facing rebrand/de-jargoning, and environment
unification (dev / staging / production on one migration head with isolated data).

## 1. Where it stands

| | |
| --- | --- |
| Migration head | `0064_free_tier_defaults` (via `0063_qa_result_producer_run`) |
| `REQUIRED_SCHEMA_REVISION` | `0064_free_tier_defaults` |
| Development DB | `video_platform` @ 0064 (compose PostgreSQL) |
| Staging DB | `video_platform_staging` @ 0064 — **new**, same server, fully separate data |
| Production DB | migrates to 0064 on deploy (`alembic upgrade head` at api start) |
| Registry config | `phase2-model-infrastructure-v7` |

One request in the operator's instructions could not be taken literally: it asked for
all three environments at head `00**_flow_remote_owner_index`. `0060_flow_remote_owner_index`
has not been the head since 0061/0062 landed (pricing corrections), and this session
added 0063/0064. All three environments unify on the *actual* head instead; nothing was
downgraded.

## 2. The three Character Evidence defects — closed

**`maximum_id_switches_for_decision: 0` is enforced.**
`resolve_track_selection` (services/character-evidence/character_evidence/pipeline.py)
now counts *attributable tracks* per character — a track whose face evidence passes the
identity threshold, or, with no usable face contradicting it, whose whole-body
appearance passes the appearance threshold. Attributable tracks beyond the first are
identity switches; more switches than the configured budget (0) makes the report
`TRACKING_UNCERTAIN` with reason `ID_SWITCH_LIMIT_EXCEEDED` → ABSTAIN, regardless of
score margins or crossing. This is the reappearing-person case that used to PASS on a
fraction of the evidence. Note the Modal deployment still runs the old image until it
is redeployed — see §6.

**Concurrent callbacks converge on one QAResult.** `qa_results` gained
`producer_run_id` plus a unique `(candidate_id, producer_run_id)` index (0063, with a
backfill from `metrics_json`; pre-existing duplicates keep a NULL run id as history).
`QAPipeline.validate_candidate` now does the completed-run check, the QAResult insert
and the run-id append in **one transaction under a `FOR UPDATE` lock on the candidate
row**; the unique index is the backstop, and a loser of that race returns the winner's
row instead of raising or duplicating. The JSON lost-update window is gone with it.

**`evaluate_promotion` validates the dataset it judges.** It now takes the full dataset
document (`dataset_version`, `authorization_record`, `examples`) and enforces
`validation/dataset.schema.json` in code — required fields, types, enums, the
authorization record's owner/purpose/retention, and every example's
`consent_record_id`. Any violation returns `eligible: false` with typed failures
(`EXAMPLE_CONSENT_MISSING:…`, `AUTHORIZATION_RECORD_INVALID:…`) *before* metrics are
computed, and an APPROVED plan cannot override it. A test pins the code's field lists
against the published schema so they cannot drift.

The Modal async-handler warnings are also fixed: the ASGI endpoint now awaits
`Dict.put.aio` / `spawn.aio` (verified present on the installed modal 1.5.4) instead of
calling the blocking client on the event loop.

## 3. FREE-plan hard gates (all server-side)

- **Chat/reasoning**: every FREE role binding already pointed at
  `doubao-free-reasoner`; 0064 repoints that definition from the
  `CONFIGURE_DOUBAO_MODEL_ID` placeholder to **`doubao-seed-2-0-lite-260428`** and
  enables it (guarded on the placeholder so an operator-configured value is never
  overwritten). FREE resolution stays fail-closed: disable the model and FREE gets
  `LookupError`, never the paid catalogue.
- **Images**: new FREE `IMAGE_GENERATION` binding → `seedream-5.0-ark`
  (**`doubao-seedream-5-0-260128`**). The old "FREE image generation is unavailable"
  refusal is gone; FREE image requests route through the FREE catalogue and can only
  ever land on that model.
- **Image quality tiers**: the browser sends `image_tier` = `shiny` / `shinier` /
  `shiniest`; `IMAGE_MODEL_TIERS` in `entitlement_core.admission` maps them to
  `seedream-5.0-ark` / `flow-narwhal-image-internal` (NARWHAL) / `gpt-image-2-openrouter`.
  Pro tiers **deny** FREE workspaces (403), never redirect. A tier whose model is
  disabled (e.g. no OpenRouter credential) refuses as temporarily unavailable. NARWHAL
  remains unpriced/disabled per the pricing handover — selecting it will refuse at the
  quote until that changes; it is listed as a locked Pro tier, which is the honest state.
- **Usage limits** (`Settings`: `free_plan_max_images=3`,
  `free_plan_max_director_turns=10`, `free_plan_max_prompt_optimizations=5`):
  - *Images*: total per FREE workspace, counted from `generation_jobs` (image jobs not
    FAILED/CANCELLED); idempotent replays exempt; `image_count > 1` denied outright.
  - *Director rounds*: per creative session, counted from USER turns, enforced in
    `CreativeDirectorService._user_turn` (`CreativeTurnLimitReached` → 403).
  - *Deep prompt optimization*: `workspace_usage_counters` row (0064), locked
    `FOR UPDATE`, incremented before the refine and handed back if the refine fails.
  - **Interpretation note for the operator:** the instruction did not state a window.
    Images and optimizations are implemented as *totals per FREE workspace* (upgrade
    lifts them); director rounds as *per session*. Changing any of this is a Settings
    edit (or a small query change for a rolling window).

`tests/test_free_tier_gates.py` pins all five gates end-to-end through the real API
with `auth_required=True`.

## 4. UI: rebrand and de-jargoning

- "AI Director" → **"BestShiny Director"** across nav, page titles, buttons and the
  public site.
- New-project dialog no longer pre-seeds "Vertical short drama" — standard placeholder.
- New **image quality dropdown** (✨ Shiny — Free / ✨✨ Shinier 🔒 — Pro / ✨✨✨
  Shiniest 🔒 — Pro), locked options disabled for FREE, taglines per tier, no model IDs.
- Raw provider model IDs no longer render anywhere user-facing: `MODEL_LABELS` +
  `friendlyModel()` map IDs to public names (video dropdown, action bar, result bar,
  job detail); unknown IDs fall back to "Studio model".
- `CR` → `credits` everywhere (pill, wallet, productions, public site); USD amounts and
  per-second/per-image rates removed from user surfaces ("About N credits" /
  "Quoted before you generate").
- Payment copy dropped PaymentIntent / order_ref / webhook / signed-callback wording.
- QA variant summaries are humanized (`HARD_FAIL: IDENTITY_DRIFT` → "hard fail:
  identity drift"); style-lock copy no longer mentions embeddings or version hashes.
- Temperature / top-p: **no surface in this product exposes sampling parameters** (UI
  or backend contract), so there was nothing to replace; nothing fake was added.

## 5. Environments

- Development and staging live on the same compose PostgreSQL server as separate
  databases (`video_platform`, `video_platform_staging`), both at 0064, zero shared
  rows. Point at staging with
  `DATABASE_URL=postgresql+psycopg://video_platform:<pw>@127.0.0.1:5432/video_platform_staging`.
- The dev api/worker images were rebuilt after the migration (§2.34 built-image hazard).
- Production picks up 0063/0064 through the normal deploy (extract → build → `up -d`,
  api runs `alembic upgrade head`). The 0064 data step is guarded and idempotent.

## 6. Not proven / left open

- The id-switch enforcement and async-handler fix are **not live on Modal** until
  `modal deploy services/character-evidence/modal_app.py` runs; the deployed image
  still has the old selection logic. Everything else in §2 is BestShiny-side and live
  on deploy.
- Character Evidence has still never run on real authorized media; no signed callback
  has ever been observed end-to-end (unchanged from previous handovers).
- The payment framework remains parked per the operator (2026-08-30); its copy changed,
  its behavior did not.
- Live spend: nothing in this session called a paid provider. The live E2E plan and its
  cost require operator approval before execution.
