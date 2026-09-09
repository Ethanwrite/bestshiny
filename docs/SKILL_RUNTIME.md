# The Skill runtime — registered is not resolved, resolved is not invoked, invoked is not Skill-driven

Recorded 2026-09-08 on branch `claude/bestshiny-skill-runtime-audit-533387`. This document is the entry
point for how a Skill reaches a model on this platform. The code is `core/skills/skill_core/runtime.py`
(the runtime), `core/skills/skill_core/registry.py` (the registry with machine-readable bindings), and the
five call sites named in the matrix below. The test suite that proves it is `tests/test_skill_runtime.py`.

## 1. What was wrong

All twelve `skills/*/SKILL.md` bodies were registered (`SkillRegistry`, `GET /v1/skills`, the admin
catalogue) and displayed, but the registry was not the runtime:

- only the **director** body ever entered a model call - creative turns and the screenplay (story *and*
  shots) both ran under it, with its version and hash on the row;
- the **prompt-compiler** body was resolved for its hash and never injected: `compile_input()` was
  deterministic and the record still said "prompt-compiler: sha256:…";
- **shot decomposition, cinematography design and continuity review had no Skill call at all**: the
  Director decided every shot's states and gaze, the compiler used locked-off camera defaults, and the
  continuity engine was a rules vector;
- the episode continuation called the DIRECTOR model with a hard-coded prompt (no Skill, no version);
- the image-prompt corrector's record claimed `"image-prompt-corrector": "v1"`, a version the registry never
  held;
- a model failure fell back to the deterministic path and the row still carried the Skill's version as if
  the Skill had written the outcome.

**Skill Registry ≠ Runtime Invocation.** A Skill is bounded authority + a runtime binding + a versioned
instruction + a structured contract, not a Markdown file.

## 2. The unified flow

```text
operation ──resolve──▶ one Skill (registry metadata: role/stage/operations/model_role)
          ──bind────▶ EXPECTED_BINDINGS[operation] == skill.role, else the build fails
          ──load────▶ system prompt = skill body + the stage's application protocol (one body, ever)
          ──snapshot▶ name / version (sha256:…) / content_hash / role / stage on the invocation
          ──invoke──▶ ModelRoleRuntime.execute_chat(project, skill.model_role, messages)
          ──validate▶ the stage's contract (pydantic) + authority checks
          ──track───▶ resolved / loaded / model_invoked / execution_mode / fallback_reason
```

`SkillRuntime.invoke(operation, project_id=…, protocol=…, messages=…, validator=…)` is the only path by
which a body reaches a model. It refuses a message list that carries any other installed body ("one
operation loads one Skill"), so injecting the catalogue is impossible by construction. Every call returns a
`SkillInvocation`:

| Field | Meaning |
| --- | --- |
| `resolved` | the operation mapped to exactly one installed Skill with the expected role |
| `loaded` | that Skill's body became the system prompt of a model call |
| `model_invoked` | the model was actually called under the Skill's `model_role` |
| `execution_mode` | `MODEL` (validated model output), `MODEL_WITHOUT_SKILL` (the generic prompt answered because the Skill was unavailable), `DETERMINISTIC` (the caller's deterministic path produced the outcome) |
| `fallback_reason` | why the model did not produce the outcome: `MODEL_RUNTIME_NOT_CONFIGURED`, `SKILL_UNAVAILABLE`, `SKILL_STAGE_DISABLED`, `MODEL_BUDGET_REFUSED`, `MODEL_UNAVAILABLE`, `MODEL_CALL_ERROR`, `MODEL_OUTPUT_INVALID`, `AUTHORITY_VIOLATION`, or a caller reason such as `PREFLIGHT_NOT_COMPILABLE`, `NO_FRESH_SKILL_COMPILATION`, `NO_PREVIOUS_SHOT`, `STORY_DETERMINISTIC` |
| `skill_driven` | `execution_mode == MODEL and loaded` - the only state in which an outcome is the Skill's work |
| `name`, `version`, `content_hash`, `role`, `stage`, `model_role` | the identity of what was loaded (or resolved) |
| `execution_record_id`, `context_hash`, `reason_codes`, `validation_errors`, `authority_violations`, `raw` | the audit of the call |

The row columns `skill_version` / `skill_content_hash` (creative turns and screenplay revisions) now mean
*what was loaded into the call*; they are null when no body was loaded. The full invocation rides in
`context_json.skill_invocation` (turns), `content_json._context.skill_invocations.{story,shots}`
(screenplays), `shots.cinematography_json.skill_invocation`, `decision_records.input_features.skill_invocation`
(`CONTINUITY_REVIEW`, `CINEMATOGRAPHY_DESIGN`) and `prompt_compilations.diff_json.skill_invocation`.

## 3. Bindings (machine-readable, in each SKILL.md frontmatter)

| Operation | Skill | Role | Stage | Model role | Contract |
| --- | --- | --- | --- | --- | --- |
| `creative_conversation` | director | director | STORY | DIRECTOR | DirectorTurnResult |
| `story_generation`, `story_revision` | director | director | STORY | DIRECTOR | StoryDraft |
| `shot_decomposition`, `shot_revision` | short-drama | shot_planner | SHOT_PLANNING | SHOT_PLANNER (new) | ShotPlan |
| `cinematography_design` | cinematography | cinematography | CINEMATOGRAPHY | CINEMATOGRAPHY_REASONING | CinematographyPlan |
| `continuity_review` | continuity | continuity | CONTINUITY | CONTINUITY_REASONER | ContinuityReview |
| `prompt_compilation` | prompt-compiler | prompt_compiler | PROMPT_COMPILATION | PROMPT_COMPILER | PromptCompilerOutput |

The frontmatter carries `role`, `stage`, `runtime` (`model` / `reference`), `operations`, `model_role`,
`authority`, `forbidden_authority`, `output_contract` and, for reference Skills, `bound_to`. The registry
refuses an authority/forbidden overlap, a reference Skill claiming an operation, a model Skill without a
model role, and two Skills claiming one operation or one runtime role. `SkillRuntime.validate()` runs at
container build and fails it when the installed registry does not satisfy `EXPECTED_BINDINGS`.

The seven reference Skills are consulted as method, never injected: `lighting`, `camera-movement` and
`composition` are sub-domains folded into Cinematography (lighting is deliberately not a separate agent);
`commercial` informs the Director's product invariants; `character-consistency` informs Continuity and the
identity lock; `model-prompting` is adapter-stage method; `image-prompt-corrector` is the user-facing
deterministic corrector. Their records say `execution_mode: DETERMINISTIC`, `loaded: false`.

## 4. Responsibility boundaries, enforced

| Stage | Owns | Must never decide | Enforcement |
| --- | --- | --- | --- |
| Director (WHY) | intent, hook, promise, invariants, variables, emotional and creative visual direction, forbidden zones, required dialogue, ending, final decision | shot count, start/end state, gaze, composition, framing, lens, focal length, movement, lighting, model, provider | `StoryDraft` has no shot fields; forbidden keys are stripped and recorded (`AUTHORITY_STRIPPED:<key>`); the story is kept |
| Shot Planner (WHAT HAPPENS) | shot boundaries, one dominant action, subject, line placement, start/end state, gaze target, spatial state, continuity handoff, duration intent, mobile hook check | dialogue text, characters, ending, identity, product facts, shot size, framing, lens, movement, light, model | every story line placed once, verbatim, in order; unknown character, unplanned or invented beat → the plan is rejected whole (`AUTHORITY_VIOLATION:dialogue_changed:beat n`); photographic keys and `shot_type` stripped; a micro-action outside the closed vocabulary, a malformed `micro_actions` field or a fifth micro-action is dropped and recorded (`MICRO_ACTION_DROPPED:<value>`), never fatal |
| Cinematography (HOW WE SEE IT) | shot size, framing, composition, angle, height, position, one movement, lens intent, DOF, focus, lighting, contrast, colour temperature, exposure, atmosphere | plot, action, dialogue, characters, product facts, ending, states, boundaries, continuity verdict, model, provider | a plan carrying a forbidden key is refused as a whole (`AUTHORITY_VIOLATION:<path>`); deterministic defaults stand in; string limits bound prompt size (400 characters for focus / path / position / lens intent, 800 for a composition), not wording |
| Continuity | identity/wardrobe/prop/geography/screen-direction/gaze/body-state/entrance-exit/lighting/axis continuity, canonical binding, end→start contract, `PASS / REPAIRABLE / ESCALATE`, minimal repair | framing, movement, lighting design, composition, action, dialogue, ending, identity version creation, canonical promotion, model, provider | a review carrying a forbidden key is refused as a whole; the deterministic state comparison stands in |
| Prompt Compiler | prompt wording and ordering, negative prompt, asset echo, one assertion per fact, QC checklist, compilability verdict | plot, action, dialogue, composition, framing, camera, lighting, unresolved creative fields, asset invention, model, provider, vendor syntax | a deterministic preflight (two actions, two movements, any `unresolved`/`tbd` value, `unresolved:` constraints) returns `NOT_COMPILABLE` before any model call; a model package is re-verified (asset echo, assertion count, action / subjects / line / claims / copy verbatim, no provider or model name, no vendor syntax, no envelope key) and discarded on failure |

Rule of thumb the code follows: an *upstream* stage's out-of-authority extras (Director, Shot Planner
photographic keys) are stripped and recorded, because the stage's own fields are intact; a *downstream*
stage's out-of-authority fields (Cinematography, Continuity, Compiler) refuse the whole output, because
a plan that also rewrote the action reasoned from a different action.

## 5. Two-stage authoring

The screenplay is now written by two Skills. The Director's story call (`STORY_PROTOCOL`) returns a
`StoryDraft`: treatment, invariants, variables, characters, scenes, beats with their required `dialogue`
lines in order, product claims, required copy with its beat, obligations, unresolved. The Shot Planner's
call (`SHOT_PLAN_PROTOCOL`, model role `SHOT_PLANNER`, bound to sonnet primary / opus fallback / doubao
FREE) receives the locked story and returns a `ShotPlan`. `merge_story_and_shot_plan` folds them into the
`Screenplay` every later stage already reads; shot size is `MEDIUM` (undetermined) or `DIALOGUE` (a speaking
shot) until Cinematography designs the shot and writes its framing back onto `shots.shot_type`.

Screenplay reasoners: `MODEL:DIRECTOR+MODEL:SHOT_PLANNER` (both Skill-driven),
`MODEL:DIRECTOR+DETERMINISTIC:SHOT_PLANNER` (the planner fell back to one line per shot; the row's
`deterministic` flag is true and approval needs `accept_deterministic`), `DETERMINISTIC` (the scaffold).
Shot-stage reason codes ride on the row prefixed `SHOT_PLANNER:`.

Invariant versioning: every revision records `_context.invariants` - `version` (`inv:…`, a hash over
identity, relationships, scene geography, required dialogue, ending, invariants, product claims, required
copy and obligations), `supersedes_version` when one of those classes moved, `unchanged_from_version`
otherwise, and `changed`. The previous row is never rewritten; it is the baseline. The API exposes
`invariant_version`, `supersedes_version`, `invariant_changes`.

## 6. Stages after beats approval

`POST /v1/creative/sessions/{id}/beats/approve` compiles the episode as before and then runs
`VisualStageRunner.run_episode`: cinematography per shot (`shots.cinematography_json`, `shot_type`
written back when Skill-driven, a `CINEMATOGRAPHY_DESIGN` decision), continuity per adjacent pair (a
`CONTINUITY_REVIEW` decision on the target shot), prompt compilation through the Skill (a
`prompt_compilations` row with `execution_mode: MODEL` and `input_hash`). The report rides on the response as
`skill_stages`. Each stage fails on its own and is recorded; none can undo the compile. The switch is
`FEATURE_SKILL_STAGES_AT_APPROVAL` (default on); off, every stage records `SKILL_STAGE_DISABLED`.

`POST /v1/shots/{id}/generate` first runs the prompt-compiler Skill for exactly the envelope the autopilot
is about to compile (`VisualProductionRuntime.compiler_inputs`: the request's character bindings, the
project's canonical assets, the resolved dependencies); a package that is still fresh is reused, not paid for
again. The synchronous generation path (`PromptCompilerService.compile`) then reuses the Skill's package
when a `MODEL` record exists for exactly the current envelope hash (recorded as `reused_from`), compiles
deterministically otherwise (`NO_FRESH_SKILL_COMPILATION`), and honours a fresh Skill verdict of
`NOT_COMPILABLE` by refusing with the Skill's reason - a shot changes, the verdict expires with the hash.
The compiler reads the cinematography plan between the timeline state and a caller's explicit overrides.

Per-shot routes: `POST /v1/shots/{id}/cinematography`, `GET /v1/shots/{id}/cinematography`,
`POST /v1/shots/{id}/continuity/review`, `POST /v1/shots/{id}/prompt/compile`,
`POST /v1/episodes/{id}/skill-stages`, `GET /v1/skills/runtime` (bindings + matrix + problems).

**Cost.** Under `PROVIDER_MODE=live`, every stage call is a small paid text call: one per shot for
cinematography and compilation, one per adjacent pair for continuity, on top of the story and shot-plan
calls that replaced the single screenplay call. A seven-shot episode is about twenty small calls at approval,
under the daily production budget breaker. `scripts/skill_runtime_matrix.py` prints the matrix;
`scripts/review_skill_contract.py` now checks the ten sections and the binding of every Skill.

## 7. The matrix

| Skill | Registered | Runtime Bound | Body Injected | Structured Validated | Fallback Tracked |
| --- | --- | --- | --- | --- | --- |
| director | yes | yes | yes | DirectorTurnResult, StoryDraft | yes |
| short-drama (shot_planner) | yes | yes | yes | ShotPlan | yes |
| cinematography | yes | yes | yes | CinematographyPlan | yes |
| continuity | yes | yes | yes | ContinuityReview | yes |
| prompt-compiler | yes | yes | yes | PromptCompilerOutput | yes |
| lighting (folded into cinematography) | yes | no | no | no | no |
| camera-movement (folded into cinematography) | yes | no | no | no | no |
| composition (folded into cinematography) | yes | no | no | no | no |
| commercial (folded into director) | yes | no | no | no | no |
| character-consistency (folded into continuity) | yes | no | no | no | no |
| model-prompting (adapter-stage reference) | yes | no | no | no | no |
| image-prompt-corrector (user tool, deterministic) | yes | no | no | no | no |

Five Skills are integrated (Runtime Bound = yes and Body Injected = yes). The seven reference Skills are
reported honestly as registered only. `GET /v1/skills/runtime` computes the same rows from the live
registry, with the per-process invocation counts beside them.

## 8. Live check, 2026-09-09 (dev)

One paid session was run on the dev stack (rebuilt at `1494e72`, database at `0082`, `PROVIDER_MODE=live`)
through a temporary development-bypass API against the dev database: two director turns, two accepted
assumptions, a brief approval (story + shot plan), and on a script-compiled three-shot episode one
cinematography design, one continuity review and one Skill compile. Seven model calls, all `SUCCEEDED`,
USD 0.0113 in total under the daily breaker. **Every role resolved to `doubao-seed-2-0-lite-260428`**
(provider `seedance`): the project created under the bypass received a workspace on the FREE plan, and a
FREE plan resolves only the FREE bindings - so this exercised exactly what a free-plan user gets, not the
opus / sonnet / gpt-5.6-sol / qwen bindings a PRO workspace resolves to. What the reason codes said:

| Call | Model role | Outcome | Reason codes / record |
| --- | --- | --- | --- |
| Turn 1, turn 2 | DIRECTOR | `MODEL:DIRECTOR`, Skill-driven | `SKILL_LOADED, MODEL_REPLY`; the protocol's own `EVIDENCE_UNVERIFIED`, `OPERATIONS_REJECTED`, `SKIP_UNVERIFIED` - the model paraphrased the client's words as evidence, so the critical fields landed as assumptions and the brief stayed CLARIFYING until they were accepted (the pre-existing residual of §2.46) |
| Story | DIRECTOR | `MODEL`, Skill-driven, no stripped keys | `SKILL_LOADED, MODEL_REPLY`; one original story, one scene, five beats |
| Shot plan | SHOT_PLANNER | `DETERMINISTIC` fallback, tracked | `SHOT_PLANNER:MODEL_OUTPUT_INVALID, SHOT_PLANNER:ScreenplayInvalid, SHOT_PLANNER:DETERMINISTIC_FALLBACK`: five shots carried micro-actions outside the closed vocabulary (`slight_head_tilt`, `slight eyebrow raise`, `slow shallow breathe`, `gaze widening`, `finger tremor slightly`). Reasoner `MODEL:DIRECTOR+DETERMINISTIC:SHOT_PLANNER`, `deterministic: true`; the conformance gate also raised `SCREENPLAY_CONTRADICTS_BRIEF` (`LOCATION_CHANGED`: a paraphrased location) |
| Cinematography | CINEMATOGRAPHY_REASONING | `DETERMINISTIC` fallback, tracked | `MODEL_OUTPUT_INVALID, ValidationError`: `camera.focus` was a 200-character focus-pull description against a 160-character limit |
| Continuity | CONTINUITY_REASONER | `MODEL`, Skill-driven | verdict `ESCALATE`, zero mismatches, `approval_required`: no registered `END_FRAME` evidence for the source shot and its cinematography was the deterministic fallback - "absent evidence is never a pass", applied as written |
| Prompt compile | PROMPT_COMPILER | `MODEL`, Skill-driven | verdict `NOT_COMPILABLE`: the first shot of a script-compiled episode has an empty `subjects` array (the actor enters the timeline state only *after* the first action), no eyeline, no props; the Skill refused what the deterministic path would have compiled |

What this says about the contracts, not the runtime: the runtime resolved, injected and recorded exactly
one Skill per call with its version and hash, and every rejection was tracked as a fallback rather than
passed off as the Skill's work. Four follow-ups came out of it; three landed the same day: (1) an unknown
micro-action is advisory - dropped and recorded as `SHOT_PLANNER:MICRO_ACTION_DROPPED:<value>` (a string
field is read as a comma-separated list, a malformed field is dropped by type, a fifth canonical key is
dropped as surplus) and the plan keeps the planner's staging; the schema stays strict for user edits;
(2) the cinematography string limits bound prompt size rather than wording (focus / path / position /
lens intent 400, framing / angle / height / DOF 200, compositions 800, atmosphere 600); (3) a
script-compiled first shot takes its actor and the acted-upon prop from its output state - the prompt
carries no claim about where they were at the shot's start, and the compilation record's
`derived_from_output_state` says where the compiler learned them; a Skill `NOT_COMPILABLE` still blocks
generation of such a shot, by design, with the reason in the 409. A package the Skill produced before
this change no longer matches such a shot's envelope hash, so its first generation after the change
compiles deterministically (`NO_FRESH_SKILL_COMPILATION`) until the stage reruns. (4) is the PRO-workspace
run recorded in §9.

## 9. Live check under a PRO workspace, 2026-09-09 (dev, after the fixes)

The same flow, with the project's workspace moved to the PRO plan, on the tree carrying follow-ups (1)-(3).
Eight calls, USD 0.69; all `SUCCEEDED`; the bindings a paying user resolves to answered.

| Call | Model | Outcome |
| --- | --- | --- |
| Turn 1 | claude-opus-5 | `MODEL:DIRECTOR`, Skill-driven; the brief was proposable after one turn (`SKILL_LOADED, MODEL_REPLY`, plus the protocol's `EVIDENCE_UNVERIFIED` / `OPERATIONS_REJECTED` on a paraphrased quote) |
| Story | claude-opus-5 | `MODEL`, Skill-driven, no stripped keys; six beats, title 她的名字亮着 |
| Shot plan, first attempt | claude-sonnet-5 | `DETERMINISTIC` fallback, tracked: `SHOT_PLANNER:MODEL_OUTPUT_INVALID` - the reply stopped at exactly the 6,000-token output cap (`completion_tokens = 6000`), so the JSON was cut. Reasoner `MODEL:DIRECTOR+DETERMINISTIC:SHOT_PLANNER` |
| Cinematography | gpt-5.6-sol | `MODEL`, Skill-driven; the plan named four unresolved points that are real for a script-only shot (empty gaze target, empty cast list, empty start state, no canonical bindings) and framed its shot size as "provisional" |
| Continuity | claude-sonnet-5 | `MODEL`, Skill-driven, `ESCALATE` with zero mismatches: no registered `END_FRAME` evidence for a `CONTINUOUS` transition |
| Prompt compile | gpt-5.6-sol | `MODEL`, Skill-driven, `NOT_COMPILABLE`: "`shot_spec.camera.framing` is marked as provisional and therefore remains unresolved" - the compiler read the cinematography plan's hedge as an unresolved field, which is its Unresolved Policy applied as written; the first shot now carried its subject 雨桐 and the phone (`derived_from_output_state`) |
| Redraft after the cap fix (story revision + shot plan) | claude-opus-5 + claude-sonnet-5 | **`MODEL:DIRECTOR+MODEL:SHOT_PLANNER`, `deterministic: false`, `skill_driven: true`.** The plan ran to 7,898 output tokens: eight shots over six beats, one action per shot, every line placed verbatim, every gaze target named, micro-actions inside the vocabulary (`gaze_shift`, `blink`, `breathe`, `mouth_movement`, `slight_head_turn`). Invariant versioning recorded `supersedes_version` with `changed: [invariants, obligations]` |

Changes made from it: `STORY_MAX_OUTPUT_TOKENS = 12000` and `SHOT_PLAN_MAX_OUTPUT_TOKENS = 16000` (the caps
bound a runaway reply, not a normal one), and the runtime records `MODEL_OUTPUT_TRUNCATED` beside the parse
failure when the provider's `finish_reason` is `length`, so a cut reply is diagnosed as a cap rather than as
a model that cannot follow the contract. The conformance gate's `SCREENPLAY_CONTRADICTS_BRIEF`
(`LOCATION_CHANGED`: the screenplay describes the brief's location in more words) is the pre-existing
brief gate, not the runtime; approval takes `accept_brief_violations`.

Open after this run: for a script-only episode (no director intent) the cinematography stage hedges and the
compiler Skill refuses, so generation of such a shot through the Skill path stays blocked by design until
the shot is given a gaze target and a cast or the stage is rerun; a creative-director episode carries
those in its director intent. Spend for both checks: USD 0.70 under the USD 10 daily breaker.
