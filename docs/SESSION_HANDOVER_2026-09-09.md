# Session handover — 2026-09-09 (the Skill runtime's content-quality acceptance)

**Entry point: [`SKILL_RUNTIME.md`](SKILL_RUNTIME.md) §10.** This file records what the session did, what it
verified and what it left open. Branch `claude/skill-optimization-quality-dd745b`, worktree
`.claude/worktrees/production-creation-deletion-f848a1`, based on `main` `342c5d0` (production at `74beba5`
= #65 on #64). No migration: the alembic head stays `0082_shot_cinematography_plan`.

## 1. The finding

The merged #64 + #65 tree kept the framework - unified invocation, split responsibilities, tracked fallbacks,
five runtime Skills and seven reference Skills - and failed the content-quality acceptance on five points the
reviewer reproduced by simulation. All five reproduced on `342c5d0` here as well (one throwaway test per gap,
each deleted after it failed for the right reason), and each is now pinned in `tests/test_skill_quality_gates.py`:

| # | Gap | Where it was |
| --- | --- | --- |
| 1 (P1) | the prompt-compiler Skill's package never reached the provider payload | `services/production-engine/production_engine/runtime.py` `prepare_autopilot` (the adapter received the spec alone), `core/adapters/video_adapter_core/{base,adapters}.py` (prompt rebuilt from `canonical_lines`, fixed negative prompt) |
| 2 (P1) | a Skill-driven continuity `ESCALATE` + `approval_required` was recorded, never enforced | `core/continuity/continuity_core/review.py` (a decision row only), `core/video-prompt/video_prompt_core/compiler.py` (compiled regardless), `core/production/director_production/stages.py`, `POST /v1/shots/{id}/generate` |
| 3 (P1) | unresolved photography compiled; a Skill plan's `unresolved` entries never entered the envelope | `compiler.py` `_preflight` (six prose paths + four subject fields, exact markers only), `_envelope` (never read `plan.unresolved`) |
| 4 (P2) | height, lens intent, depth of field, motivation, exposure intent, subject placement, atmosphere, compositions dropped | `compiler.py` `_envelope` (eight explicit camera reads, a five-key lighting filter), `packages/contracts/platform_contracts/shot.py` (no fields to carry them) |
| 5 (P2) | the fresh-package cache ignored the Skill version | `compiler.py` `_fresh_skill_compilation` (envelope hash only) and `_record` (the installed version written over an older Skill's package); `cinematography.py` `_persist` (a fallback reused an older Skill's plan with a decision naming the new version) |

## 2. What changed

- `core/adapters/video_adapter_core/base.py` — `AdapterInput.package` (+ `skill_package`), `prompt_lines`,
  `negative_prompt`, `BASELINE_NEGATIVE_TERMS`; the Camera line prints `angle` and, when set, height / lens /
  depth of field; `Atmosphere:` and `Composition:` lines when set; empty `motivation` / `exposure_intent`
  pruned. `adapters.py` — every adapter delivers `prompt_lines` and a package-aware `_result`.
- `packages/contracts/platform_contracts/shot.py` — `CanonicalCameraSpec.height / lens_intent /
  depth_of_field`, `CanonicalLightingSpec.motivation / exposure_intent`, `CanonicalShotSpec.atmosphere /
  composition`, `identity_key` (public); `cinematography.py` — `camera_values` / `lighting_values` carry the
  new keys.
- `core/video-prompt/video_prompt_core/compiler.py` — `ContinuityGateSource`, `ContinuityApprovalRequired`,
  `PromptCompilerService(continuity=…)`; `_envelope` maps the whole plan (subject placements through
  `identity_key`, applied / kept / unmatched on `cinematography_notes`) and carries a Skill-driven plan's
  `unresolved` entries as constraints; `compile()` gates, then reuses only a package the installed Skill
  produced, honours a Skill refusal across Skill edits; `compile_shot_with_skill()` gates before reuse and
  re-asks the model when the Skill changed; `_fresh_skill_compilation` → `_FreshPackage` (producing
  version / hash, `current`); `_record` names the producing Skill and the installed one; `_preflight`
  covers every photographic field with markers and hedges and names `cinematography.unresolved[n]`;
  `to_neutral_prompt` and the checklist render the design when set. Exported from `video_prompt_core` and
  `skill_core`.
- `core/continuity/continuity_core/review.py` — `input_hash` on every review decision,
  `ACKNOWLEDGEMENT_DECISION_TYPE`, `ContinuityAcknowledgementConflict`, `pending_escalation`,
  `ensure_reviewed`, `gate_view`, `acknowledge`; `review_pair` returns `approval_required` and `input_hash`.
- `core/production/director_production/cinematography.py` — a fallback that reuses an older Skill's plan
  records `SKILL_VERSION_CHANGED:<old>` and the plan's provenance; `stages.py` — `approval_required` on the
  report, `decision_record_id` on continuity entries.
- `services/production-engine/production_engine/runtime.py` — the package to the adapter,
  `metadata.prompt_delivery` / `prompt_package`, `_stored_prompt_package` on retry.
- `apps/api/video_platform_api/container.py` (the reviewer built first and handed to the compiler),
  `main.py` (the gate before planning; 409 `CONTINUITY_APPROVAL_REQUIRED` mapping), `skill_routes.py`
  (`GET /v1/shots/{id}/continuity/review`, `POST …/continuity/review/acknowledge`, the 409 mapping).
- `skills/{prompt-compiler,continuity,cinematography}/SKILL.md` — the bodies say what the runtime does with
  their output (delivery, freshness, enforcement, entries that block). Their hashes moved.
- Tests: `tests/test_skill_quality_gates.py` (new, 15).
- Docs: `SKILL_RUNTIME.md` §4 / §6 / §10, `OPEN_ISSUES.md` §2.53, `CURRENT_ARCHITECTURE.md`, `HANDOFF.md`,
  `README.md`, `docs/README.md`.

## 3. Decisions worth knowing

1. **Deterministic continuity reviews are advisory.** Only a Skill-driven escalation gates. Otherwise every
   development stack without a continuity model, and every production outage, would block shots on a
   comparison that never read the director's text; and a fallback re-review can never release a verdict the
   Skill gave (it stays pending with the fallback reason on the 409).
2. **Replay-stable metadata.** The gateway hashes request metadata for idempotency, so `prompt_delivery`
   carries no record id; a same-key request after an escalation was recorded, or across a deterministic →
   Skill compile, is a 409 by design.
3. **The Skill's prompt is the body; the adapter adds what the contract leaves out** (the locked style, any
   unrestated continuity assertion, the bounded context, its model line) and merges the baseline negative
   guards rather than replacing the Skill's negative prompt.
4. **Hedges are scanned in photographic fields only**, word-bounded for Latin tokens; prose (the action, the
   line, a pose, the staging, constraints) is never scanned, so the deterministic scaffold's "placeholder
   staging" keeps compiling.
5. **A Skill edit does not lift a refusal.** A Skill `NOT_COMPILABLE` for the current envelope blocks the sync
   path until the new Skill answers for that envelope.
6. **An older Skill's cinematography plan survives a fallback** (a real design beats locked-off defaults);
   the decision says so.

## 4. Gates

Run from the worktree on the main checkout's venv (worktree code takes precedence through pytest's
`pythonpath`; `scripts/review_skill_contract.py` needs `PYTHONPATH` built from the same list to resolve the
worktree's `video_prompt_core`).

| Gate | Result |
| --- | --- |
| `ruff check .` | clean |
| `python -m mypy` | clean, 214 source files |
| `scripts/review_skill_contract.py` on the three edited Skills | 3 × PASS |
| `pytest -q tests/test_skill_quality_gates.py` | 18 passed |
| targeted suites (`test_skill_runtime`, `test_video_adapters`, `test_cinematography_contract`, `test_compiler_first_shot_subjects`, `test_video_prompt_compiler`, `test_installed_skills`, `test_visual_runtime`, `test_provider_payload_contracts`, `test_project_style_lock`, `test_pipeline_semantic_consistency`, `test_director_shot_intent`, `test_screenplay_invariants`, `test_director_api`, `test_episode_continuation`, `test_dependency_context_pipeline`, `test_audit_p1_p2_fixes`, `test_creative_director`, `test_creative_visual_retry`, `test_director_canonical_assets`, `test_compile_refusal_ordering`, `test_bible_lock_resume`) | 296 passed, 1 skipped |
| `pytest -q` (SQLite half, whole tree, detached) | 1840 passed, 20 skipped, exit 0 (10m35s), rerun after the review fixes |
| `pytest -q --database=postgres` (whole tree, detached) | 1853 passed, 7 skipped, exit 0 (23m41s), rerun on the final tree |

## 4a. The adversarial review of the fixes

Five reviewers (compiler, gate, delivery, contracts/docs, tests) went over the diff before it was committed,
each finding verified by three skeptics. Sixteen findings; ten changed the branch, and they are listed in
`docs/SKILL_RUNTIME.md` §10. The one that mattered most: **the Skill path delivered the package and dropped
`spec.constraints`** - the client's prohibitions, the forbidden items and the director's approved staging -
because only the action, subjects, line, claims and copy are re-verified in a package's prose. A fix that
made the paid compiler's wording reach the provider would have quietly stopped delivering the client's own
instructions. Two more were about money and truthfulness: a fallback continuity re-review re-ran on every
generate attempt, and the cinematography stage's own write-back sat inside its reuse key, so one Skill design
was never recognised again and the next fallback overwrote it (that one shipped in #64).

## 5. Not verified here

1. **No live check.** No paid call was made; the delivery, the gate and the freshness rule are proven with the
   scripted model doubles. The first live generate after deploy pays one prompt-compiler call per shot (every
   stored package is stale) and, for a shot whose previous shot has no registered end frame, meets the
   continuity gate exactly as the 2026-09-09 PRO run did - now as a 409 rather than a record.
2. **No web affordance for the acknowledgement.** The 409 detail names the route and the decision; the shot
   inspector shows decision records but has no "approve this handoff" control yet.
3. **Cost.** Unchanged in kind: one continuity re-review when a pending escalation's inputs changed, one
   prompt-compiler call per shot after a Skill edit.
