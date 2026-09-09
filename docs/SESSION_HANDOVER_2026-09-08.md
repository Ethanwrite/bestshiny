# Session handover — 2026-09-08 (the Skill runtime: registered is not invoked)

**Entry point for the Skill work: [`SKILL_RUNTIME.md`](SKILL_RUNTIME.md).** This file records what the
session did, what it verified, and what it left open. Branch
`claude/bestshiny-skill-runtime-audit-533387`, worktree `.claude/worktrees/bestshiny-pr42-architecture-fix-c2f3d1`,
based on `main` `6078e55` (production at `60d7764`). Alembic head moves to
`0082_shot_cinematography_plan` (one JSON column on `shots`; the deployed api refuses to start until
`alembic upgrade head` runs - stop the worker across the DDL as usual).

**Merged and deployed 2026-09-09:** PR #64 squash-merged as `1494e72` ≈10:01Z and deployed to
production ≈10:12Z (`DEPLOYED_SHA.prev = 60d7764`, alembic `0082`, `DEPLOY_EXIT=0`, verified; record in
`docs/DEPLOYMENT.md` §6). **The contract fixes from the live checks followed as PR #65, squash `74beba5`,
deployed ≈12:12Z** (no migration, `DEPLOYED_SHA.prev = 1494e72`; `docs/SKILL_RUNTIME.md` §8-§9).

## 1. The audit, in one table

| Skill | Before | After |
| --- | --- | --- |
| director | body injected for turns and the whole screenplay (shots, states, gaze included); version on rows even when the outcome was the deterministic fallback | body injected for turns and the *story* only through `SkillRuntime`; shot fields stripped and recorded; version on a row means "loaded into the call"; fallback rows say `execution_mode: DETERMINISTIC` |
| short-drama | registered, never invoked | the Shot Planner: `shot_decomposition` / `shot_revision` under the new `SHOT_PLANNER` model role; a plan that changes a line, invents a character or leaves a beat unplanned is rejected whole |
| cinematography | registered, never invoked; the compiler used locked-off defaults | `cinematography_design` per compiled shot under `CINEMATOGRAPHY_REASONING`; plan on `shots.cinematography_json`, framing written back to `shot_type`, read by the compiler |
| continuity | registered, never invoked; a rules vector decided continuity mode | `continuity_review` per adjacent pair under `CONTINUITY_REASONER`; `PASS / REPAIRABLE / ESCALATE` recorded as a `CONTINUITY_REVIEW` decision; deterministic state comparison as the fallback |
| prompt-compiler | resolved for its hash, never injected | `prompt_compilation` under `PROMPT_COMPILER` with preflight and re-verification; the generation path reuses a fresh package by envelope hash |
| lighting, camera-movement, composition | registered | reference Skills folded into cinematography (declared `bound_to`); reported as not runtime-bound |
| commercial, character-consistency | registered | reference Skills folded into director / continuity |
| model-prompting, image-prompt-corrector | registered; the corrector record claimed `"v1"` | reference Skills; the corrector record now carries the real snapshot with `loaded: false` |

The episode continuation, which called the DIRECTOR model with a hard-coded prompt, now runs under the
director Skill through the runtime with the invocation on `brief.provenance`.

## 2. What changed

- `core/skills/skill_core/registry.py` — frontmatter metadata parsed into `SkillDefinition` (`role`,
  `stage`, `runtime`, `operations`, `model_role`, `authority`, `forbidden_authority`, `output_contract`,
  `bound_to`, `sections`); registry-wide binding validation.
- `core/skills/skill_core/runtime.py` — `SkillRuntime`, `SkillOperation`, `SkillInvocation`,
  `EXPECTED_BINDINGS`, `CALL_SITES`, `integration_matrix()`; the single injection path.
- `skills/*/SKILL.md` — all twelve industrialised: Purpose, Pipeline Position, Inputs, Authority,
  Forbidden Authority, Invariants, Decision Rules, Escalation Rules, Output Contract, Unresolved Policy.
  Content hashes changed for all twelve.
- `core/creative-director/…` — `StoryDraft`, `ShotPlan`, `validate_story`, `validate_shot_plan`,
  `shot_plan_violations`, `merge_story_and_shot_plan`, `deterministic_shot_plan`, invariant versioning
  (`invariant_record`); `STORY_PROTOCOL` and `SHOT_PLAN_PROTOCOL`; the service reasons the screenplay in
  two stages through the runtime; views expose `skill_invocation(s)`, `skill_driven`, `invariant_version`,
  `supersedes_version`, `invariant_changes`.
- `core/production/director_production/{cinematography,stages}.py`,
  `core/continuity/continuity_core/review.py`, `core/video-prompt/video_prompt_core/compiler.py`,
  `core/episode-continuation/…/service.py`, `packages/contracts/platform_contracts/{cinematography,continuity}.py`.
- `core/model-registry/model_registry_core/schemas.py` + `config/model-registry/defaults.json` —
  `ModelRole.SHOT_PLANNER` (capability `assistant_director`; sonnet primary, opus fallback, doubao FREE;
  bindings seed on boot), registry version `v9`.
- `apps/api/video_platform_api/{container,main,skill_routes,creative_routes}.py` — the runtime in the
  container (build fails on a binding problem), `GET /v1/skills` with bindings, `GET /v1/skills/runtime`,
  per-shot stage routes, stages after beats approval, the corrector record fix.
- `packages/shared/platform_shared/config.py` — `feature_skill_stages_at_approval` (default on).
- `apps/web/app.js` — reasoner labels for the two-stage screenplay, stage labels on the revision line and
  the director meta, the admin catalogue shows role / stage / bound-or-reference.
- `scripts/skill_runtime_matrix.py` (new), `scripts/review_skill_contract.py` (sections + binding checks).
- Tests: `tests/test_skill_runtime.py` (new, 28), `tests/test_creative_director.py` (two-stage double),
  `tests/test_installed_skills.py` unchanged in intent.

## 3. Gates

Run from the worktree on the main checkout's venv (worktree code takes precedence through pytest's
`pythonpath`; `alembic check` with `PYTHONPATH` built from the same list). Numbers are filled in below as
the runs completed.

| Gate | Result |
| --- | --- |
| `ruff check .` | clean |
| `python -m mypy` | clean, 214 source files |
| `alembic upgrade head` + `alembic check` (scratch SQLite) | `0082` applies; "No new upgrade operations detected" |
| `scripts/review_skill_contract.py skills/*/SKILL.md` | 12 × PASS (two advisories: `model-prompting` and `image-prompt-corrector` bind to nothing, by design) |
| `apps/web` `vite build` | built (`node --check app.js` clean) |
| `pytest -q` (SQLite half) | see §3a |
| `pytest -q --database=postgres` | see §3a |

### 3a. Suite results

| Run | Result |
| --- | --- |
| `pytest -q` (SQLite half, whole tree, detached) | 1801 passed, 3 failed, 20 skipped, 22m30s. The three: `test_asset_registry.py::test_0008_*` ×2 (migration `0082` used SQLite batch mode, which recreates `shots` and trips the character-state trigger; rewritten as a guarded plain `ADD COLUMN` like `0073`) and `test_screenplay_brief_conformance.py::…cannot_be_approved` (asserted the old single-stage reasoner string). All three rerun green on the final tree (`36 passed` for the two migration suites, `41 passed` for conformance + audit fixes). |
| Suites touched after that run, rerun on the final tree (SQLite) | `test_skill_runtime` 28 passed; `test_creative_director`, `test_shot_contract_v2`, `test_creative_director_cast_coverage`, `test_episode_continuation`, `test_installed_skills`, `test_video_prompt_compiler`, `test_model_routing_integrity`, `test_plan_provider_budget`, `test_image_prompt_corrector`, `test_prompt_refine_fact_lock`: 97 passed; `test_director_api`, `test_free_tier_gates`, `test_domain_api`: green; `test_auth_tenancy`, `test_passenger_model_selection`, `test_web_session_boundaries`: 61 passed |
| `pytest -q --database=postgres` on the suites this session touched (`test_skill_runtime`, `test_creative_director`, `test_screenplay_brief_conformance`, `test_asset_registry`, `test_migration_history`, `test_director_api`, `test_video_prompt_compiler`, `test_free_tier_gates`, `test_episode_continuation`, `test_installed_skills`), final tree | **146 passed, exit 0** (6m32s) |
| `pytest -q --database=postgres` (whole tree, detached, started before the three fixes above) | 1814 passed, 3 failed, 7 skipped, 44m14s - the same three tests as the SQLite half, all fixed and rerun green on PostgreSQL in the row above |

## 4. Not verified here

1. **Live check done 2026-09-09 on dev** - see `docs/SKILL_RUNTIME.md` §8. Seven paid calls (USD 0.011),
   all answered by the FREE binding `doubao-seed-2-0-lite` because the project's workspace is on the FREE
   plan (what a free user gets); the director turns and the story were Skill-driven, the shot plan and the cinematography plan
   fell back on record (closed micro-action vocabulary; a 160-character `focus` limit), continuity
   escalated on absent end-frame evidence, the compiler refused a subject-less first shot. The three
   contract fixes and a PRO-workspace rerun followed the same day (`docs/SKILL_RUNTIME.md` §8-§9): under
   PRO, opus / sonnet / gpt-5.6-sol answered, every stage was Skill-driven once the shot-plan output cap was
   raised (the first plan was cut at 6,000 tokens), and the compiler still refuses a script-only first shot
   whose cinematography plan hedges - by design.
2. **Cost.** Beats approval is now about 2N+1 small paid text calls per N-shot episode under live mode,
   and a generation request pays one prompt-compiler call when no fresh package exists for its exact
   envelope (a second request with the same bindings reuses it). `FEATURE_SKILL_STAGES_AT_APPROVAL=false`
   turns the three stages and the pre-generation compile off (recorded as `SKILL_STAGE_DISABLED` /
   `NO_FRESH_SKILL_COMPILATION`); the Director and Shot Planner calls are not gated.
3. **No browser session** exercised the new labels; the bundle builds and the script parses.
4. The deterministic continuity review compares timeline states only; the textual director states are
   the model's to read.
