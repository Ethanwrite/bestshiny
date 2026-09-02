# Router Evidence — four layers, one posterior, and a flag that is off

Snapshot: 2026-08-26 · companion to [`../HANDOFF.md`](../HANDOFF.md) and
[`../CURRENT_ARCHITECTURE.md`](../CURRENT_ARCHITECTURE.md)

This document describes the production learning loop for the model router: where evidence comes
from, what is allowed to combine with what, how the posterior is computed offline, how a replay
decides whether it may ever affect routing, and why exploration ships switched off with no switch.

The short version, for someone deciding whether to read on:

- **Nothing here changes routing today.** `feature_router_lcb` is `False`, and with it off the
  router receives byte-for-byte the evidence it received before this existed.
- **Almost no external evidence reaches the posterior**, and that is the rule working rather than
  a gap: there is no calibration bridge between any public benchmark's scale and any production
  outcome's scale, so the layers stay independent.
- **`model_metrics` is untouched.** The adaptive router still reads it and behaves as before.
  `router_observations` is a second, wider record written alongside it.

---

## 1. The four layers

    official_prior        what the vendor says about its own model
    benchmark_prior       what a third party measured, with a stated protocol
    community_prior       what practitioners report having experienced
    production_posterior  what this platform actually observed

They are separate because they fail differently. A vendor's own number is optimistic in a
predictable direction. A benchmark is honest about a task that may not be yours. A community
report is a real observation of a real failure with an unknown denominator. Only the fourth is
measured on this platform's own traffic.

**Physical separation, not convention.** The three external layers are frozen JSON files under
`config/router-evidence/`, one per layer, each with its own schema and its own loader; a loader
refuses a file whose declared layer is not the one it was asked for. Production observations are
database rows. `EvidenceLayerStore` deliberately offers no `all_records()` and no `merged()` — a
test asserts the absence — so there is no object that can hold two layers' records at once.

| Layer | File | Source types | May become a prior? |
|---|---|---|---|
| official | `config/router-evidence/official-v1.json` | technical report, model card, docs, pricing, release, changelog | grade A/B, exact-version match |
| benchmark | `config/router-evidence/benchmark-v1.json` | academic paper, independent benchmark, arena leaderboard, third-party benchmark | grade A/B, exact-version match |
| community | `config/router-evidence/community-v1.json` | reddit, x, github issue/discussion, huggingface, discord, forum, creator comparison | grade A/B/**C**, first-hand or paraphrased, not marketing/bot/duplicate |
| production | `router_observations` table | this platform | it *is* the posterior |

Community evidence admits grade C and the other layers do not. A benchmark on a grade C source is a
screenshot of a number; a practitioner's report is *inherently* grade C — one person, one venue, no
protocol — and holding it to the benchmark bar would exclude the whole layer while leaving it on
disk. Credibility still multiplies the weight, so a C post counts for half of a B one. Grade D stays
out everywhere.

---

## 2. The isolation key

Every number is addressed by `EvidenceKey`:

    provider · model_id · exact_version · task_type · scenario · metric_scale_id

Two numbers may only meet if their keys are equal. Production observations additionally carry a
`ConditionBucket` — duration bucket, resolution, reference mode — which splits the leaf.

`exact_version` deserves a note. It is `ModelCapabilityProfile.version` — for example
`wan-2.7-manual-v4` — the exact *configuration* this platform can vouch for. Where a provider's
model id carries a dated snapshot (`doubao-seedance-2-5-260628`) the pair really does pin the
weights. Where the id is an alias (`google/veo-3.1`) it does not, and that is what
`model_is_alias` is for: such an observation is quarantined rather than attributed, because
pooling outcomes from before and after a silent repoint is precisely the contamination this work
exists to prevent.

**Scales never meet.** Each production outcome has its own `prod.`-prefixed scale id, which
structurally guarantees no production posterior can share a scale with an external benchmark.
`calibration.BRIDGES` is empty as of 2026-08-26 and `may_pool()` therefore returns false for every
cross-scale pair. Adding a bridge is a research act with a source, an anchor count and a date.

---

## 3. The production observation contract

One row per generation attempt in `router_observations`. Conditions:

    provider · model_id · exact_version · model_is_alias
    task_type (T2V/I2V/R2V/V2V/T2I/I2I/R2I)
    scenario (14 scenes, plus `generic` for a holistic measurement)
    asset_criticality · prompt_complexity · reference_mode
    duration_seconds · resolution · aspect_ratio

Outcomes:

    delivery   generation_success · provider_failure · latency_ms · cost_credits · cost_usd
    human      user_rating (1-5) · user_preference_ab (win/loss/tie) · regenerated ·
               switched_model · downloaded · accepted_output · used_in_next_shot
    automated  qc_identity_score · qc_motion_score · qc_prompt_alignment ·
               qc_temporal_consistency  (each 0-1)

Two rules the schema and the database both enforce:

- **`None` means not observed, never zero.** A shot nobody rated must not become a one-star.
- **A failed generation carries no quality score.** Nothing was produced, so there is nothing to
  judge; `ck_router_obs_failed_has_no_quality` rejects the row. Allowing it would let a provider
  outage read as a quality problem and teach the router to avoid a good model permanently.

Writes are idempotent on `generation_job_id`, so a retried worker or a replayed webhook cannot
inflate the counts the LCB gate reads.

**Latency and cost have no posterior.** They are unbounded continuous quantities and a Beta over
them would be a category error. They are summarised in their own units, with nearest-rank
percentiles, by `CostLatencySummary`.

---

## 4. The hierarchical posterior

One Beta posterior per cell, computed offline by `scripts/router_posterior_run.py`. Five levels:

    L0  GLOBAL      a fixed, weakly-informative prior — Jeffreys (0.5, 0.5) — not learned
    L1  VERSION     this exact configuration, all tasks and scenes
    L2  TASK        ...on this task type
    L3  SCENARIO    ...in this scene
    L4  CONDITION   ...at this duration bucket, resolution and reference mode

Each level is estimated from its own data and then used as a bounded prior for the level below,
at `kappa = 4–6` pseudo-observations. A scene with plenty of data barely moves; a scene with three
observations sits close to its task's behaviour and says so through a wide interval.

**Why cross-version inheritance is impossible.** L1 is the highest level that touches a model, and
there is no level above it that mixes models. L0 is a *constant*, not an estimate over other
models — specifically so it cannot become a back channel between versions. Veo 3.1's data cannot
shrink Veo 3.1 Fast's posterior because there is no shared parent for them to meet in.

The child's prior is the parent's shrinkage **plus** the global prior, never instead of it. Without
that floor, a cell whose parent is already near certainty inherits a near-zero pseudo-count on one
side, and thirty consecutive successes then produce a Beta with `b` below 0.01 — an interval of
`[0.99999, 1.0]` that every later comparison treats as fact. Keeping Jeffreys' half on each side
means no cell can claim more certainty than its own data supports.

`strict_isolation=True` sets every pooling strength to zero, leaving each cell nothing but its own
data and the fixed global prior. Partial pooling is a modelled relationship between things that
really are related; someone may reasonably want none of it.

Each saved cell carries: `posterior_mean`, `posterior_lower_quantile`, `posterior_upper_quantile`,
the two quantile levels, `effective_sample_size`, `observation_count`, `alpha`/`beta`,
`prior_alpha`/`prior_beta`, `prior_sources`, `prior_version`, `parent_level`, `parent_mean`,
`calculated_at` and `engine_version`.

One arithmetic note that cost a database constraint: `ck_router_posterior_ordered` checks
`lower <= upper` and says nothing about the mean. For a heavily skewed Beta — the shape a cell with
a long run of identical outcomes takes — the mean can lie outside its own central interval. A
constraint saying otherwise rejects correct arithmetic.

### Community effective sample size

Twenty posts about a model losing a face can be twenty people, one person and nineteen reposts, or
one marketing account and nineteen replies quoting it. The count says twenty in all three cases, so
nothing reports a count. `CommunityAggregator` reports an effective sample size after: exact
duplicates collapsed by content hash, declared duplicates dropped, marketing and suspected
automation dropped, second-hand reports dropped and paraphrases discounted to 0.35, and repeated
posts by one author decayed harmonically — the second counts for half, the third for a third.
Twenty posts by one person come to about 3.6; twenty posts by twenty people come to 20.

Filtering happens before the decay, deliberately: a marketing account's three posts must not consume
the first three slots and push a real user's report down to a third of a vote. Engagement is
recorded and never used as a weight — a viral post is one observation that many people saw.

---

## 5. Why external evidence does not reach the production posterior

`production_contributions()` offers every eligible external prior to each production outcome and
records a `RefusedContribution` for each one it declines. Today it declines all of them, with
reason `NO_CALIBRATION_BRIDGE`: a VBench 0.939 and a `prod.accepted-output` rate are not two
readings of one quantity, and no published artefact establishes the exchange rate.

This is the mandate's own rule — independent posteriors where there is no bridge — reaching its
conclusion. The mechanism is built and exercised so that the day a bridge exists, the priors flow
through a reviewed path rather than one written in a hurry. The refusals are printed, because a
silent empty dictionary looks identical to "there was no evidence".

External evidence is therefore, today, a **reporting** layer: coverage, conflicts, and the answer to
"why is this model's prior still a hand-authored number".

---

## 6. Research: Grok searches, this repository refuses

    scripts/research_router_evidence.py   → data/router-evidence/raw/<layer>/<model>.json
    scripts/ingest_router_evidence.py     → config/router-evidence/<layer>-v1.json
                                          + data/router-evidence/ingest-report.json

The search runs in two stages. Stage one asks Grok to search the web and write prose findings —
unconstrained, because structured-output mode makes the model emit the whole JSON object on every
turn and pushes it towards answering before it has searched. The first probe of this pipeline came
back with an empty record list, one turn, and complete confidence. Stage two converts the prose into
records with `--disable-web-search`, so it physically cannot introduce a fact stage one did not find.

Two operational notes worth keeping: `--permission-mode dontAsk` cancels the run the first time a
tool wants approval and the transcript reads as a model that simply stopped searching, so
`--always-approve` is what actually lets a search finish; and the answer schema is generated from the
same Pydantic models the ingest validates against, so the research contract and the validation
contract cannot drift.

`/data/` is gitignored, so the raw responses and the ingest report are local
working artefacts. The committed record is the three layer files, which carry
the accepted records, the gaps, and the researcher's closing notes — enough to
see what was searched and what was found missing without the transcripts.

`EvidenceIngestor` then refuses:

| Reason | What it catches |
|---|---|
| `UNQUOTED_NUMBER` | a value whose `verbatim_quote` does not contain the number — the highest-yield check there is, because a fabricated score cannot point at itself |
| `MISSING_PROVENANCE` | open-web evidence with no URL |
| `SCALE_UNKNOWN` | a `metric_scale_id` outside the registered set |
| `VERSION_UNCONFIRMED` | HIGH mapping confidence without the source naming a version |
| `RETRIEVED_BEFORE_PUBLISHED`, `FUTURE_TIMESTAMP` | impossible dates |
| `VALUE_OUT_OF_SCALE` | a value outside its own declared scale — a percentage filed as a 0-1 ratio |
| `SOURCE_TYPE_UNRESOLVED` | no valid source type, and the URL does not settle one |
| `SCHEMA_INVALID` | including a sample size, interval or generation count present without `*_stated_by_source` |

and *marks* rather than refuses: `ALIAS_AS_SNAPSHOT`, `NO_SAMPLE_SIZE`, `EVALUATION_METHOD_UNSTATED`,
`SPAM_SIGNALS`, `MARKETING`, `BOT_SUSPECTED`. Marked records are kept and can never become priors —
near-miss evidence is kept on purpose, because a deleted one gets re-derived from the same public
page in six months and attached silently to the wrong version.

The ingest normalises exactly four things and judges none: the layer (derived from the source type),
the binding's identifiers (from the research target, so evidence cannot be attached to a model
nobody here operates — while `source_model_name` and `source_model_version` keep the source's own
words), the retrieval timestamp (this run's clock), and a community post's content hash and spam
signals. `version_match` and `mapping_confidence` are never touched.

Scales are registered in two groups. **Scoring scales** carry something a model can be better or
worse at; only the bounded ones can become a prior. **Descriptive scales** carry a documented fact —
a duration ceiling, a reference-image limit, a published price, an API enum, or `unscored` for a
claim made in words — and every one is unbounded, which is what stops any of them ever becoming a
prior. They are the most trustworthy external evidence there is and the least useful for ranking.

---

## 7. Replay: the gate

`ReplayHarness` splits history **chronologically** — never randomly, because a random split leaks
the future into the fit — fits the posterior on the earlier window and scores on the later one.

Only the model that actually ran has an outcome for a given shot, so policies are compared on
*context buckets*: within one bucket every model with observations has an empirical mean, and a
policy's score is the mean of the arm it chose. A bucket where the chosen arm has no observations
cannot be scored and is reported as `unscored_contexts` rather than filled in with a guess. This is
the direct method with the assumptions the direct method always has; the mitigation is the bucket
definition, which is fine enough that "the easy shots" is a much smaller category than it would be
platform-wide.

Five quantities, reported for both policies side by side and never combined into one score:
**interval coverage, regret, generation cost, failure rate, quality outcome**.

Coverage checks the **posterior predictive** interval, not the posterior. The distinction matters
and getting it wrong makes a well-calibrated posterior look overconfident: the posterior interval
describes uncertainty about the underlying rate, while the observable is a mean over a finite
evaluation window that carries its own sampling error on top. The fitted Beta is widened by
`E[p(1-p)]/n_eval` and re-matched before its quantiles are taken.

`ReplayResult.passed` requires all of:

- regret no higher than the baseline's;
- failure rate no higher — a policy that buys quality with reliability has not passed;
- mean cost no more than 5% above — buying quality with money is a product decision, so replay
  refuses to make it silently;
- coverage determinable (at least 8 checkable cells) and within 10 points of nominal — a policy
  that beats the baseline while its intervals are badly calibrated beat it by luck;
- at most half the contexts unscored.

`failure_reasons()` says which of those failed. A gate that cannot say why is a gate people route
around.

---

## 8. The conservative LCB

`ConservativeLcbBuilder` offers the router the **lower quantile** of the relevant cells instead of
the mean, for the model, task, scenario and conditions of one request. A model with a great average
and four observations has a low lower bound and does not win on the strength of four observations.

**It does not change the router.** `VideoModelRouter` already accepts per-request `RoutingEvidence`;
this produces one, through the existing `production_adjustments` channel. No line of the ranking code
moved. `test_router_lcb_runtime_gate.py` pins the router's version, `rank`'s signature and its four
scoring profiles.

> **2026-09-01 amendment.** The router itself later changed — deliberately, by operator direction,
> not by this evidence system: `video-router-v3` selects within a hand-authored scene-champion table
> (`config/model-registry/scene-champions.json`) after the deterministic hard filter, and open
> scoring became the fallback. Nothing in *this* document's contract moved: evidence is still
> per-request, the LCB still arrives through `production_adjustments`, the flag is still off until a
> replay passes, and the version pin in `test_router_lcb_runtime_gate.py` was bumped alongside the
> intentional change (that test now exists to catch the *next* silent drift). Champion order is
> additionally protected by its own rule: production evidence can demote a champion below its
> fallback only with ≥ `min_demotion_samples` observations on **both** sides and a blended-score gap
> above `demotion_margin` — so the sufficiency discipline here and the demotion discipline there
> agree on the same 20-observation floor. See `CURRENT_ARCHITECTURE.md` § Model capability and role
> runtime for the v3 selection semantics.

Three documented fallbacks, each returning the caller's evidence object unchanged:

1. `router_lcb` flag off — the default;
2. no posterior run saved;
3. no cell sufficient for these models (≥20 observations, ESS ≥10, interval width ≤0.55).

The collapse from the six-part evidence key to the router's `provider:model_id` happens once, for
one request, at the moment of use — never during aggregation. A candidate whose exact version the
registry cannot name is skipped rather than matched to a neighbour.

Where two outcomes map to one router dimension, the **more pessimistic** wins. Averaging them would
be averaging across metrics.

`qc_prompt_alignment` is the conspicuous omission from the dimension map. It is a genuine quality
signal and the router simply has no prompt-adherence dimension to receive it. Inventing a mapping to
`visual_quality` would move a score for a reason nobody could later reconstruct, so the outcome is
recorded, given a posterior, and left out of routing until a dimension exists for it.

### Enabling it

Two switches and one precondition, in order:

```bash
# 1. compute a posterior and replay history; exit 0 means the replay passed
.venv/bin/python scripts/router_posterior_run.py

# 2. only then, per project or globally
FEATURE_ROUTER_LCB=true            # environment default
# or the database override, via the feature flag service: router_lcb
```

Exit codes: `0` passed · `2` fewer than 20 observations, no replay · `3` replay ran and did not
pass · `4` contamination found, do not enable.

---

## 9. Exploration is closed, and there is no switch

`ExplorationPolicy` ships as a design and an offline simulator. There is no feature flag, because a
flag is something someone can turn on and the absence of any call site is not.
`test_router_exploration_offline.py` asserts that no module under `services/`, `apps/`, `agents/` or
`providers/` imports it or mentions its exported names.

Six constraints, all of which must pass, and every failure is reported rather than just the first:

    budget            a credit ceiling for the window
    criticality       CANONICAL, HERO and IMPORTANT are never experiments
    cost              a per-generation cap
    minimum evidence  a model with no evidence is an unknown, not a promising arm
    failure ceiling   observed provider-failure rate above the ceiling excludes it
    eligibility       an allowlist of exact versions — not model ids, so a silent
                      snapshot change cannot inherit permission

The bonus is the *upper* quantile, the mirror of the LCB, so the two can be compared honestly in
simulation. `ExplorationSimulation.online` is hard-coded `False` and is a field rather than an
omission, so any report generated from a simulation says in its own data that it did not happen.

---

## 10. Operating it

```bash
# research (spends Grok credits; --list and --dry-run spend none)
.venv/bin/python scripts/research_router_evidence.py --list
.venv/bin/python scripts/research_router_evidence.py --layer benchmark_prior

# validate and file; the rejection report is the point
.venv/bin/python scripts/ingest_router_evidence.py
.venv/bin/python scripts/ingest_router_evidence.py --report-only

# compute, replay, report
.venv/bin/python scripts/router_posterior_run.py --dry-run
.venv/bin/python scripts/router_posterior_run.py
```

Read the current state over HTTP:

```
GET /internal/models/router-evidence     # four layers, coverage, conflicts, LCB state
GET /internal/models/external-evidence   # the older frozen registry, unchanged
```

The `lcb` block of that response answers the question an operator actually has: is the lower bound
affecting routing right now, and if not, which of the three fallbacks is in force.

---

## 11. What this does not do

- It does not replace `model_metrics` or the adaptive router.
- It does not average across benchmarks, scales, metrics or versions. Averaging happens in exactly
  one place: two readings of the *same* scale, for the *same* key, in the *same* layer.
- It does not turn a community stance into a benchmark value. Stance lives on
  `community-stance-net` and nothing bridges it to anything.
- It does not let a research assistant supply a sample size, a confidence interval, a version
  mapping or an API alias correspondence. Each is schema-gated on an explicit
  "the source stated this" flag.
- It does not explore.

---

## 12. What the first research pass actually found

Run 2026-08-26 with the Grok CLI over eleven models × three layers.

| | official | benchmark | community |
|---|---|---|---|
| candidate records returned | 108 | 79 | 68 |
| accepted | 64 | 38 | 52 |
| rejected | 44 | 41 | 16 |
| prior-eligible after acceptance | 32 | 22 | 38 |
| gaps recorded | 4 | 5 | 5 |

Source distribution of what was kept:

```
official    official_docs 33 · official_release 13 · official_pricing 11 ·
            official_benchmark 4 · model_card 1 · changelog 1 · technical_report 1
benchmark   arena_leaderboard 19 · academic_paper 14 · independent_benchmark 3 ·
            third_party_benchmark 2
community   reddit 27 · x 24 · huggingface_discussion 1
```

Per model, at exact version:

```
model                                        official  benchmark  community  community  scenes with
                                             rec/elig  rec/elig   records    ESS        eligible evidence
grok:grok-video@grok-video                     11/5       5/0        11      27.0       11
openrouter:google/veo-3.1-fast@veo-3.1-fast    11/2      11/2         5      10.0        7
openrouter:google/veo-3.1@veo-3.1              10/0      10/8         9      15.0       11
openrouter:kling-v3.0-pro@kling-v3.0-pro       12/11      0/0         0       0.0       15
openrouter:kling-v3.0-std@kling-v3.0-std        0/0       0/0         4      19.0       11
openrouter:gpt-image-2@gpt-image-2              0/0       9/9         0       0.0        7
openrouter:grok-imagine-video@…                 6/3       2/2        11      27.0       15
seedance:doubao-seedance-2-5-260628@2.5        12/11      0/0         0       0.0       11
seedance:doubao-seedream-5-0-260128@5.0         0/0       0/0        12      29.0        9
wan:wan-2.7@wan-2.7                             2/0       1/1         0       0.0        8
```

**Insufficient evidence:** `kling-v3.0-std` and `seedream-5.0` — community stance only, no
admissible official or benchmark record, and no production observations.

**Unconfirmed versions:** 47 records bind to a version the source did not confirm. They are kept
and can never become priors.

**Conflicts marked:** 37 — 31 `WITHIN_SCALE_DISAGREEMENT` (two admissible readings of one scale for
one key that do not agree, e.g. Veo 3.1's I2V Arena Elo spanning 1085–1397 across two snapshot
dates) and 6 `COMMUNITY_STANCE_SPLIT` (first-hand reports evenly divided, e.g. Grok Imagine on
identity). None is resolved.

**External priors admitted to the production posterior: 0**, all 112 refused with
`NO_CALIBRATION_BRIDGE`. **Contamination findings: 0.**

**Production observations on file: 0.** The recorder is wired into `evaluate_job` and writes one
row per evaluated generation, so the table fills as the platform is used; until it has at least 20
rows `scripts/router_posterior_run.py` exits 2 and no replay exists, which is why the LCB flag
cannot be enabled yet.

### Two things the pass got wrong, and what changed

The first community pass wrote the *layer* name into every record's `source_type` and lost all 68
records to schema validation. The repair is in two parts: `source_type` is now an enum in the
generated research schema so it is unrepresentable, and the ingest resolves a mislabelled type from
an unambiguous URL host (reddit, x, github issues/discussions, huggingface, discord, arxiv) —
normalisation, since the host is a fact about the URL. An unrecognised host stays unresolved and the
record is refused; deciding that some blog is a "creator comparison" rather than a "forum" is the
kind of guess this pipeline does not make.

The re-run to pick up that fix exhausted the account's Grok Build balance partway through, so 16 of
the 33 model×layer files are the original pass (repaired at ingest) and 11 are the corrected pass;
6 model×layer pairs have no usable research at all and are recorded as gaps. Topping the balance up
and re-running `scripts/research_router_evidence.py --overwrite` is the way to close them.

---

## 13. Defects found reviewing this work, and fixed

A review pass before merge found fifteen. One of them was the failure this package exists to
prevent, sitting inside the package, and it is worth stating plainly rather than burying in a
changelog.

**A retry inherited the previous attempt's version.** `_execute_retry` builds its metadata from
`{**metadata, ...}`, which carried `routing_context` through unchanged — `exact_version` included.
`RetryPlan` exists precisely to re-route to a *different* model, and when it did, the observation
was written with the new provider and model against the old version: a key like
`openrouter:google/veo-3.1@wan-2.7-manual-v4`, describing a pair that never ran. Nothing downstream
could catch it, because the key is internally consistent and `audit_contamination` checks a row's
key against its level, not against history. `_retargeted_routing_context` now re-resolves the
version from the registry when the target changes, and returns nothing when the registry cannot
name the new target — no observation beats a mislabelled one.

The rest, briefly, with what each would have cost:

| Defect | Consequence |
|---|---|
| `cost_credits` used truthiness, not `is not None` | a generation quoted at **zero** credits recorded as cost-not-observed; replay averages over the non-free attempts and can fail a policy on a regression that never happened |
| `round(x + 0.5)` used as `ceil` in `_percentile` | off by one whenever `fraction * n` is an integer — p90 of ten samples returned the maximum, p90 of twenty was correct; the error depended on parity alone |
| recorder caught only `ValueError` | its own docstring promises never to fail the user's request; a PostgreSQL serialization failure under concurrent evaluation would have failed one already billed |
| `latest_posterior_run_id` tiebroke on `id` (a uuid4) | which posterior snapshot the router read was not reproducible across processes |
| LCB reported `max` backing count across dimensions | a `character_consistency` bound backed by 25 observations weighted as if it had another dimension's 300 |
| freshness checked per routed request | the offline artefact back in the hot path, which is the one thing the split is for; now behind a 60s TTL |
| quote check scaled 0-1 values unconditionally | `0.87` matched "we ran 87 prompts" — a sentence about sample size validating a claimed score. A scaled match now needs the quoted number to read as a percentage (`%` or a fractional part) |
| prior strength counted value-less records | four records stating a result in words plus one number charged the full ceiling, as if five sources agreed |
| replay collapsed versions with `setdefault` | a model that changed snapshot mid-history was replayed on the superseded one; now the latest, and the notes say which models this applied to |
| over-long `observation_id` swapped for a uuid | a row stored under an identifier the caller never chose and cannot look up |
| `coverage_counts` counted in Python | an operator page load read the whole observation table to build a dozen entries |
| `resolution` unconstrained | a pipe in it would raise out of the entire posterior computation, not one cell |
| exploration simulator spent instance state | a second `simulate()` refused everything for want of a budget the first one consumed |
| ingest script's `exit_code` was dead | a pass that rejected every record exited 0 |

Thirteen regression tests, one per defect that can be pinned. Two of them are worth reading on
their own: `test_a_retry_that_re_routes_does_not_inherit_the_version` and
`test_the_percentile_is_nearest_rank_at_every_sample_count`, which checks every count from 1 to 59
rather than the two that happened to be convenient.
