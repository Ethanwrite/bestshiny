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
| Cinematography (HOW WE SEE IT) | shot size, framing, composition, angle, height, position, one movement, lens intent, DOF, focus, lighting, contrast, colour temperature, exposure, atmosphere | plot, action, dialogue, characters, product facts, ending, states, boundaries, continuity verdict, model, provider | a plan carrying a forbidden key is refused as a whole (`AUTHORITY_VIOLATION:<path>`); deterministic defaults stand in; string limits bound prompt size (400 characters for focus / path / position / lens intent, 800 for a composition), not wording; since 2026-09-09 the whole plan reaches the `CanonicalShotSpec` (`camera.height / lens_intent / depth_of_field`, `lighting.motivation / exposure_intent`, `atmosphere`, `composition.start / end`, `subject_positions` onto a subject's `screen_position` when its approved state has no explicit one) and a Skill-driven plan's `unresolved` entries refuse compilation as `cinematography.unresolved[n]` (§10) |
| Continuity | identity/wardrobe/prop/geography/screen-direction/gaze/body-state/entrance-exit/lighting/axis continuity, canonical binding, end→start contract, `PASS / REPAIRABLE / ESCALATE`, minimal repair | framing, movement, lighting design, composition, action, dialogue, ending, identity version creation, canonical promotion, model, provider | a review carrying a forbidden key is refused as a whole; the deterministic state comparison stands in; since 2026-09-09 a Skill-driven `ESCALATE` (or `approval_required`) blocks the target shot's compilation and generation until a real user acknowledges that decision (`CONTINUITY_ESCALATION_ACKNOWLEDGED`) or a later Skill-driven review replaces it, a stale escalation is reviewed again before generation, and a deterministic review is advisory (§10) |
| Prompt Compiler | prompt wording and ordering, negative prompt, asset echo, one assertion per fact, QC checklist, compilability verdict | plot, action, dialogue, composition, framing, camera, lighting, unresolved creative fields, asset invention, model, provider, vendor syntax | a deterministic preflight (two actions, two movements, a marker - `unresolved` / `tbd` / `待定` - or a hedge - `provisional` / `tentative` / `暂定` - in any photographic field: camera, lighting, subject placement / orientation / eyeline, prop values, aspect ratio; `unresolved:` constraints; the cinematography plan's own `unresolved` entries) returns `NOT_COMPILABLE` before any model call, on every path; a model package is re-verified (asset echo, assertion count, action / subjects / line / claims / copy verbatim, no provider or model name, no vendor syntax, no envelope key) and discarded on failure; the package the Skill produced is what the provider receives (§6) |

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

`POST /v1/shots/{id}/generate` first asks the continuity gate (below), then runs the prompt-compiler
Skill for exactly the envelope the autopilot is about to compile (`VisualProductionRuntime.compiler_inputs`:
the request's character bindings, the project's canonical assets, the resolved dependencies); a package that
is still fresh is reused, not paid for again. The synchronous generation path (`PromptCompilerService.compile`)
then reuses the Skill's package when a `MODEL` record exists for exactly the current envelope hash **and the
Skill that produced it is the one installed** (its `skill_invocation.content_hash`; recorded as
`reused_from`), compiles deterministically otherwise (`NO_FRESH_SKILL_COMPILATION`, plus
`SKILL_VERSION_CHANGED:<old version>` when the only difference is the Skill), and honours a Skill verdict of
`NOT_COMPILABLE` for the current envelope hash by refusing with the Skill's reason whatever Skill is installed
now - a shot changes, the verdict expires with the hash; a Skill edit does not lift a refusal, only the new
Skill's own verdict does (`compile_shot_with_skill` re-asks the model when the version differs). A record
names the Skill that produced its package (`skill_versions`, `diff_json.skill_content_hash`) and the one
installed when it was written (`diff_json.installed_skill_version`). The compiler reads the cinematography
plan between the timeline state and a caller's explicit overrides.

**Delivery (2026-09-09).** When the compilation the autopilot reuses is Skill-driven, the package is what the
provider receives: `prepare_autopilot` hands it to the adapter (`AdapterInput.package`), and every adapter
delivers `positive_prompt` as the body of its request followed by the locked style (when the project has one),
any continuity assertion the Skill did not restate in its prose, the bounded production context and the
adapter's own model-specific line(s); `negative_prompt` is the Skill's with the baseline guards it left out
(`video_adapter_core.base.prompt_lines` / `negative_prompt`). A deterministic compilation keeps the canonical
rendering. `GenerationRequest.metadata.prompt_delivery` says which (`prompt_source: SKILL_PACKAGE |
ADAPTER_CANONICAL`, execution mode, Skill name / version / content hash, envelope hash - replay-stable facts
only, because the gateway hashes the metadata for idempotency), and `metadata.prompt_package` carries the
package so an automatic retry onto another model keeps the Skill's wording and changes only the model lines.

**The continuity gate (2026-09-09).** Every `CONTINUITY_REVIEW` decision records the `input_hash` of the pair
context it was reached on. `ContinuityReviewer.pending_escalation(shot)` is the newest Skill-driven review of
the shot when it escalates (`ESCALATE` or `approval_required`) and no `CONTINUITY_ESCALATION_ACKNOWLEDGED`
decision names it; a later Skill-driven review replaces it whatever its verdict, and a deterministic review
never creates or clears one (advisory: it did not read the director's text, and a fallback must not pass off as
the Skill's verdict). `PromptCompilerService` consults the gate: `compile()` raises
`ContinuityApprovalRequired` (409 `CONTINUITY_APPROVAL_REQUIRED` with the decision, the review, whether it is
stale and how to release it), `compile_shot_with_skill()` records `NOT_COMPILABLE` with
`fallback_reason: CONTINUITY_APPROVAL_REQUIRED` and neither reuses a package nor calls the model, and
`VisualStageRunner` reports the handoffs under `approval_required`. `POST /v1/shots/{id}/generate` runs
`ensure_reviewed` first: a pending escalation whose inputs changed (a registered end frame, a redesigned plan)
is reviewed again once - a re-review that fell back keeps it pending and names its fallback reason - and a
standing one is refused before anything is planned, compiled or reserved. A real user releases it with
`POST /v1/shots/{id}/continuity/review/acknowledge` (`decision_id`, optional `note`; the development bypass
is refused with 403, as for a character state change); `GET /v1/shots/{id}/continuity/review` shows the latest
review, the pending escalation and the acknowledgements. An acknowledgement releases that decision alone and
is not re-reviewed until the stage runs again - a person may approve a verdict whose inputs have since moved,
and the acknowledgement records `stale_when_acknowledged`.

**The gate does not follow `FEATURE_SKILL_STAGES_AT_APPROVAL`.** The switch turns the *stages* off; it does
not un-say what the Continuity Skill said. With it off, a standing escalation still blocks the shot, no
re-review runs (the reviewer is disabled), and the only release is a real user's acknowledgement. Turning the
switch off during a provider incident therefore does not unblock shots that were already escalated.

Per-shot routes: `POST /v1/shots/{id}/cinematography`, `GET /v1/shots/{id}/cinematography`,
`POST /v1/shots/{id}/continuity/review`, `GET /v1/shots/{id}/continuity/review`,
`POST /v1/shots/{id}/continuity/review/acknowledge`, `POST /v1/shots/{id}/prompt/compile`,
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

## 10. Content-quality acceptance, 2026-09-09

The merged #64 + #65 tree passed its offline regressions and failed the content-quality acceptance: the
architecture routed every Skill through the runtime, but what a paid Skill decided did not always reach the
output, and what it refused did not always stay refused. Five gaps were reproduced on `342c5d0` (each by a
throwaway test, then pinned in `tests/test_skill_quality_gates.py`) and fixed on branch
`claude/skill-optimization-quality-dd745b`:

| # | Gap | Fix |
| --- | --- | --- |
| 1 (P1) | The prompt-compiler Skill's package was recorded (`prompt_compilations`, `shots.compiled_prompt`) and never delivered: `prepare_autopilot` handed the adapter the spec alone and the adapter regenerated the prompt from `canonical_lines` with a fixed negative prompt; the gateway treats `prompt` as a protected field, so `provider_payload.prompt` could not carry it either | `AdapterInput.package`; `prompt_lines` / `negative_prompt` in `video_adapter_core.base`; every adapter delivers the package body plus its model-specific lines; `metadata.prompt_delivery` (`prompt_source`, Skill identity, envelope hash) and `metadata.prompt_package`; `_execute_retry` re-delivers the stored package on a model switch |
| 2 (P1) | A Skill-driven `ESCALATE` + `approval_required` was a `CONTINUITY_REVIEW` row only: the Skill compile still ran (and paid), `compile()` still compiled, `prepare_autopilot` still prepared a request | `ContinuityReviewer.pending_escalation / ensure_reviewed / acknowledge / gate_view`, `input_hash` on every review decision, `CONTINUITY_ESCALATION_ACKNOWLEDGED` decisions; the compiler consults the gate (`ContinuityApprovalRequired`); the generate route gates before any side effect; two routes; the stage report's `approval_required` |
| 3 (P1) | `_preflight` scanned six prose paths and four subject fields for exact markers; `framing / focus / angle = TBD` compiled, and a Skill-driven cinematography plan's `unresolved` entries never entered the envelope, so the deterministic fallback compiled what the model Skill had refused | the preflight covers every camera / lighting / subject / prop field and the aspect ratio, with word-bounded hedge tokens on photographic fields only (never the action, the line, a pose, the staging or a constraint); a Skill-driven plan's entries become `unresolved: cinematography[n]: …` constraints (empty / "none" entries filtered), reported as `cinematography.unresolved[n]` with `CINEMATOGRAPHY_UNRESOLVED`; a deterministic plan's marker stays a record |
| 4 (P2) | `camera.height / lens_intent / depth_of_field`, `lighting.motivation / exposure_intent`, `subject_positions`, `atmosphere`, `start_composition / end_composition` were saved on the shot and dropped by the envelope | new fields on `CanonicalCameraSpec`, `CanonicalLightingSpec`, `CanonicalShotSpec` (`atmosphere`, `composition`); the envelope reads them; `subject_positions` match through `identity_key` and fill a subject whose approved state has no explicit `screen_position` (applied / kept / unmatched on `diff_json.cinematography`); the neutral prompt, the checklist and every adapter's lines render them only when set, so an un-designed shot renders as before |
| 5 (P2) | `_fresh_skill_compilation` matched the envelope hash only: after a Skill edit the old package was reused and the new row said `skill_versions = <new>` over `skill_invocation.version = <old>` | freshness keys on the producing Skill's content hash; a stale-version package means `SKILL_VERSION_CHANGED:<old>` (deterministic on the sync path, a new paid call from the stage); records name the producing Skill; a Skill `NOT_COMPILABLE` for the same envelope survives a Skill edit until the new Skill answers; a cinematography fallback keeps an older Skill's plan and the decision says so (`REUSED_SKILL_PLAN` + `SKILL_VERSION_CHANGED:<old>`, `reused_plan_skill_version / content_hash`) |

Decisions taken with the fixes, and their consequences:

- **Replay.** The gateway hashes `GenerationRequest.metadata` for idempotency, so `prompt_delivery` carries
  replay-stable facts only and no record id (the compilation record stays on the candidate's generation
  plan). A first attempt that compiled deterministically and a same-key retry that compiled through the Skill
  differ in prompt and conflict (409), as does a same-key replay after an escalation was recorded between the
  two requests. Worker retries never compile and are not gated.
- **Deterministic reviews are advisory.** Only a Skill-driven escalation blocks; the deterministic comparison
  still records `approval_required` (it escalates on any MAJOR mismatch of timeline states) but never gates,
  so a development stack with no continuity model and a production outage behave alike: no human is asked
  to approve a verdict the Skill did not give, and no fallback can release one it did.
- **One-time staleness at deploy.** The new spec fields change every envelope `input_hash` and every
  `metadata.canonical_shot_spec`, and the three edited Skill bodies change their content hashes: every stored
  Skill package is stale once. Under `FEATURE_SKILL_STAGES_AT_APPROVAL` a shot's next generate pays one
  prompt-compiler call; a synchronous compile before that is the deterministic package
  (`NO_FRESH_SKILL_COMPILATION`); a same-key generate straddling the deploy is a 409.
  The cinematography reuse key changes with it (the stage's own write-back left it), so an existing plan is
  not recognised on the first re-run after deploy either: that one run re-designs the shot, or overwrites the
  plan with the deterministic defaults if the model is unavailable. From the second run on, one design is
  recognised for as long as the shot's inputs hold.
- **A plan-level unresolved entry** is released by changing the shot (its intent or director intent) and
  running the stage again; a caller's camera / lighting override on `compile()` does not clear it, and a
  disabled stage reuses the Skill's plan. Not automated.
- **Prompt text.** The canonical rendering now prints `angle` on every Camera line and the design fields only
  when set; the deterministic negative prompt is unchanged.

An adversarial review of these fixes (five reviewers, three skeptics per finding) ran before the change was
committed and produced ten further corrections, all in this branch:

- **The Skill path dropped the shot's constraints** (P1). `canonical_lines` prints every constraint entry;
  the package is re-verified only for the action, the subjects, the line, the claims and the copy, so a
  client prohibition, a forbidden item or the director's approved staging could vanish exactly when the paid
  compiler wrote the prompt. `prompt_lines` now appends the constraint entries the Skill did not restate, and
  `negative_prompt(package, spec)` merges the shot's `must not show or do:` terms with the baseline guards.
  The constraint vocabulary moved to `platform_contracts.shot` so the adapters can read it.
- **`prompt_delivery` carried the installed Skill's identity on the deterministic path**, which the gateway
  hashes: editing a `SKILL.md` would have turned every in-flight idempotency key into a 409. The identity is
  recorded only when the Skill actually wrote the prompt.
- **The preflight did not scan `atmosphere` or `composition`**, the two fields gap 4 added, so both Skill
  bodies over-claimed. They are scanned now.
- **A deterministic cinematography plan filled the new camera fields**: `CinematographyCamera` gives
  `height` / `lens_intent` / `depth_of_field` non-empty defaults, so a *fallback* plan would have told the
  provider a stage decided a camera height nobody decided. Only a Skill-driven plan carries a design.
- **A fallback re-review re-ran on every generate attempt** (a paid `CONTINUITY_REASONER` call each time,
  because a fallback cannot clear staleness). The re-review now runs once per change of inputs. The
  trade-off is deliberate: when the model was down for that one attempt, a later attempt on the same inputs
  does not try again by itself, and the 409 names `POST /v1/shots/{id}/continuity/review` for a person who
  wants the verdict re-reached now.
- **The cinematography stage was not idempotent**: its own write-back (`shots.shot_type` from the plan's
  framing) was inside the reuse key, so after one design the digest no longer matched and the next fallback
  overwrote a paid plan with locked-off defaults. The write-back is excluded from the key; the Skill still
  sees the shot kind. (This one predates this branch - it shipped in #64.)
- **`_plan_unresolved_entries` swallowed real entries** beginning "None of the practicals…" / "Nothing
  about…", and numbered the constraints by their filtered position rather than their place in the plan.
- **A retry whose stored package no longer validated** delivered the canonical prompt while still recording
  `prompt_source: SKILL_PACKAGE`.
- Docs: the gate is independent of `FEATURE_SKILL_STAGES_AT_APPROVAL` (above), an acknowledgement may
  approve a verdict whose inputs have moved (recorded as `stale_when_acknowledged`), and the facts invariant
  in `CURRENT_ARCHITECTURE.md` now describes both delivery paths.
- Tests: every adapter is exercised with a package (Grok and Wan were not), the deterministic escalation is
  driven through `run_episode`, the acknowledge route's 409 and its idempotent 200 are both pinned, and the
  single-design cinematography fallback has its own test.

**The verbatim action check, after the live check (2026-09-10).** The first live production compile was
discarded with `dominant_action_missing`: the model rendered
`雨桐进入城市天台上发现了一部不属于她的手机` as `雨桐进入城市天台，发现了一部不属于她的手机。` - one particle
fewer and a comma more - and the exact-substring test made that a rewrite. The paid wording was thrown away
in favour of the deterministic JSON dump, which is the defect this whole branch exists to remove, arriving
one step further down the pipe. `action_preserved` now decides it: an exact quotation passes as before; a
run of the action's own tokens passes (only punctuation or spacing differed); otherwise the package must
carry at least 80% of the action's adjacent-token pairs, which is what survives a stylistic edit and what a
different action destroys. Tokens are words in scripts that separate them and characters in scripts that do
not, so the rule reads Chinese and English alike. A package that renders rather than quotes ships with
`ACTION_PARAPHRASED` on the invocation, so the row still says which it was. Dropping the action, swapping
its verb, or swapping its actor all still fail — those cases are pinned in
`tests/test_skill_quality_gates.py`. The line, product claims and required copy keep the exact test:
they are quoted material, not prose.

Skill bodies edited (their hashes moved): `prompt-compiler` (Pipeline Position: delivery and freshness;
Inputs and Decision Rules: the new fields; Unresolved Policy: markers, hedges and the cinematography entries),
`continuity` (Escalation Rules: the runtime enforces a Skill-driven escalation, re-reviewed once per change
of inputs; a deterministic comparison is advisory), `cinematography` (Unresolved Policy: an entry blocks
compilation; a hedge in a field is refused). No migration. Tests: `tests/test_skill_quality_gates.py` (18).
Record: `docs/OPEN_ISSUES.md` §2.53, `docs/SESSION_HANDOVER_2026-09-09.md`.

