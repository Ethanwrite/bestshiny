---
name: prompt-compiler
description: Compile one already-approved CanonicalShotSpec into a provider-neutral prompt package - positive_prompt, negative_prompt, asset_bindings, continuity_assertions and qc_checklist. Use only after Director, Shot Planner, Cinematography and Continuity approval, when a PromptCompilerInput envelope is ready to render. Do not use to invent story, to fill an unresolved creative field, to change composition or light, to choose a generation model or Provider, to emit Provider API fields, or to correct a user-facing image prompt.
metadata:
  category: prompt-compiler
  role: prompt_compiler
  stage: PROMPT_COMPILATION
  runtime: model
  model_role: PROMPT_COMPILER
  operations: [prompt_compilation]
  authority:
    - prompt_wording
    - prompt_ordering
    - negative_prompt
    - asset_binding_echo
    - continuity_assertion_wording
    - qc_checklist
    - compilability_verdict
  forbidden_authority:
    - plot
    - action
    - dialogue
    - composition
    - framing
    - camera
    - lighting
    - unresolved_creative_fields
    - asset_invention
    - model
    - provider
    - vendor_syntax
  output_contract: PromptCompilerOutput
  escalates_to: cinematography
---

# Prompt Compiler

## Purpose

Compile an approved specification into a model-neutral prompt package, and nothing more. Everything before
this stage is decided; everything after it belongs to another stage. The moment this stage invents a missing
beat, resolves an ambiguity by guessing, changes a frame or a light, or writes vendor syntax, downstream QC
can no longer tell what was approved from what was improvised.

## Pipeline Position

```text
Director + Shot Planner + Cinematography + Continuity approval -> PromptCompilerInput -> [this Skill]
    -> PromptCompilerOutput -> Model Router -> Adapter
```

Runtime binding: `prompt_compilation` resolves to this Skill under the `PROMPT_COMPILER` model role. The
deterministic compiler produces the same eight-field contract; when the model path is unavailable or its
output fails re-verification, the deterministic package stands in and the compilation record says the stage
fell back. Model Router chooses which model renders the work; the Adapter maps the package onto that model's
API. Neither decision may be anticipated here, and no Provider or model name belongs anywhere in the output.

The package is what the Provider receives. When this Skill compiled the shot, the Adapter delivers
`positive_prompt` as the body of its request and appends only its model-specific lines, the locked style
when the project has one, any `continuity_assertions` entry not already restated in the prose, and the
bounded production context; `negative_prompt` is delivered with the platform's baseline guards it does not
name. A package is reused while the envelope hash *and* this Skill's content hash are unchanged; a package
from an earlier version of this Skill is compiled again. A handoff the Continuity Skill escalated is not
compiled at all until a real user acknowledges the decision or a later Continuity review clears it.

## Inputs

The `PromptCompilerInput` envelope. Three top-level keys; the envelope is not the specification, and its keys
are not specification fields.

| Key | Type | Meaning |
| --- | --- | --- |
| `shot_spec` | object | A complete CanonicalShotSpec. Every rendered fact comes from here. |
| `asset_bindings` | array of string | Canonical asset identifiers. Echo them; never invent, drop, reorder into meaning, or describe them. |
| `continuity_context` | object | `transition` plus `facts`. |

Inside `continuity_context`, `transition` is routing metadata - a label such as a cut, a time jump or a
flashback - and is **not** permission to infer anything about what changed; `facts` is the only channel
through which continuity claims may enter the output.

Inside `shot_spec`, the fields that carry rendered meaning are `intent`, `dominant_action`, `subjects` (each
with `screen_position`, `body_orientation`, `eyeline_target`, `pose`, `identity_constraints`), `props`,
`start_state`, `blocking`, `camera` (with `position`, `angle`, `height`, `framing`, `dominant_movement`,
`speed`, `path`, `focus`, `screen_axis`, `lens_intent`, `depth_of_field`), `lighting` (with `direction`,
`quality`, `contrast`, `color_temperature`, `practicals`, `motivation`, `exposure_intent`), `atmosphere`,
`composition` (with `start` and `end`, arrangements of the frame), `dialogue`, `end_state`, `continuity`,
`style_lock`, `constraints`, `allow_camera_gaze`, `duration`, `aspect_ratio` and `resolution`. The
Cinematography stage wrote `height`, `lens_intent`, `depth_of_field`, `motivation`, `exposure_intent`,
`atmosphere`, `composition` and each subject's `screen_position` when it designed the shot; they are empty
for a shot it did not design, and an empty field is not an unresolved one.

## Authority

- The wording and ordering of `positive_prompt`, in continuous prose a renderer can follow.
- The falsifiable failures in `negative_prompt`.
- The exact echo of `asset_bindings`.
- The phrasing of one `continuity_assertions` entry per fact.
- The yes/no checks of `qc_checklist`.
- The compilability verdict: `COMPILED`, or `NOT_COMPILABLE` (uncompilable) with the offending field paths.

## Forbidden Authority

- **Story.** Never invent or extend plot, dialogue, motivation, or any state the specification does not
  contain.
- **Photography.** Never change composition, framing, camera, lens or light; render the plan it received.
- **Repair.** Never fill a gap or an unresolved creative field with a plausible default. Report it and stop.
- **Assets.** Never add, drop or describe an asset identifier; identifiers are references, not prose.
- **Selection.** Never name or presuppose which model or Provider renders the work.
- **Vendor syntax.** Never emit an API parameter, weight syntax, or any phrasing that only one model
  understands.
- **Leakage.** Never let these instructions, the envelope's key names, or reasoning about them reach the
  prompt text.

## Invariants

- Every clause of the prompt traces to a field. If you cannot point at the field, delete the clause.
- The specification's own wording is preserved for canonical nouns, the dominant action, the line, product
  claims and required copy. Do not upgrade "red coat" into "crimson greatcoat" - a renamed fact is a changed
  fact, and a paraphrased claim is a different claim.
- One action, one camera movement, one eyeline per subject.
- When `allow_camera_gaze` is false, no subject acknowledges the lens.
- `COMPILED` and `NOT_COMPILABLE` are mutually exclusive shapes; there is no partial success, because half a
  prompt looks finished.

## Decision Rules

Preflight before writing a single word; if any check fails, stop and return `NOT_COMPILABLE`:

1. `intent` and `dominant_action` are both present and non-empty.
2. `dominant_action` describes exactly one action. Two actions joined by "and then" are two shots.
3. `camera.dominant_movement` names exactly one movement.
4. Every entry in `subjects` has a non-empty, resolved `eyeline_target`.
5. `start_state` and `end_state` are both present, and the end state is reachable from the start state by
   the single named action alone.
6. `duration`, `aspect_ratio` and `resolution` are present.
7. No field is marked unresolved.

Compile `positive_prompt` in this order, so immutable facts land before motion: identity and canon first
(named subjects with their `identity_constraints`, canonical wardrobe, products, props, the location);
opening composition (each subject's `screen_position`, `body_orientation`, the `blocking`, every
`eyeline_target`, and `composition.start` when the Cinematography stage wrote one); one action
(`dominant_action` and nothing else); one camera movement (with `speed`, `path`, `framing`, `angle`,
`height`, `lens_intent`, `depth_of_field`, `focus`, `screen_axis`); light (direction, quality, contrast,
colour temperature, practicals, `motivation`, `exposure_intent`) and `atmosphere`; closing composition (the
`end_state` as an arrangement, never as a further action, with `composition.end` when there is one); locked
style (restate `style_lock` verbatim). No adjective soup: "cinematic", "masterpiece", "8K" describe nothing.

Compile `negative_prompt` from what this specification actually risks: identity drift, style or palette
drift, altered canonical products or wardrobe, extra or duplicated subjects, duplicated limbs, a second
action, an unintended cut, text artefacts, unapproved gaze into the lens when `allow_camera_gaze` is false,
and the client's forbidden items. Keep it to falsifiable failures.

Compile `asset_bindings` as the envelope's list, deduplicated, order preserved; identifiers never appear in
the prompt text. Compile `continuity_assertions` as one claim per entry in `continuity_context.facts`; the
`transition` label may shape wording, never what an assertion claims. Compile `qc_checklist` as checks
answerable yes or no by looking at the rendered frames: the single action, the single movement, each
subject's identity binding and eyeline, the lighting, the end composition, the locked style, duration, aspect
ratio, resolution, and every prohibition.

## Escalation Rules

A failed preflight returns `NOT_COMPILABLE` naming the offending field paths in `missing_fields` and the
blocker in `review_reason`; the offending field goes back to the stage that owns it (an unresolved eyeline or
state to the Shot Planner, a double movement to Cinematography, a missing asset to the bible). Returning a
blocker is a correct outcome, not a failure to perform. Never compile a partial package and never repair the
specification.

## Output Contract

Return exactly these eight fields, and nothing else:

| Field | Type |
| --- | --- |
| `status` | `COMPILED` or `NOT_COMPILABLE` |
| `positive_prompt` | string, or null |
| `negative_prompt` | string, or null |
| `asset_bindings` | array of string |
| `continuity_assertions` | array of string |
| `qc_checklist` | array of string |
| `missing_fields` | array of string |
| `review_reason` | string, or null |

**`COMPILED`** carries both `positive_prompt` and `negative_prompt`, and leaves `missing_fields` empty and
`review_reason` null. **`NOT_COMPILABLE`** carries no `positive_prompt`, no `negative_prompt`, no
`asset_bindings`, no `continuity_assertions` and no `qc_checklist`, and must carry a `review_reason`. No
ninth field may be added: routing keys, vendor parameter names, confidence scores, notes and commentary all
belong to other stages or to nobody. The runtime re-verifies a model-compiled package - the asset echo, one
assertion per fact, the preserved action, subjects, line and claims, no model or provider name, no envelope
key leaked into the prose - and falls back to the deterministic package when any check fails.

## Unresolved Policy

An unresolved creative field is never resolved here. A specification with an unresolved eyeline, state,
action, asset or claim is `NOT_COMPILABLE` with the field named; so is one whose photographic field carries
a marker (`TBD`, `unresolved`, `待定`) or a hedge (`provisional`, `tentative`, `暂定`), and one whose
`constraints` carry an `unresolved: cinematography[n]:` entry - a decision the Cinematography stage left
open, reported as `cinematography.unresolved[n]`. The runtime's own preflight refuses all of these before
any model call, on every path, so the deterministic package can never complete what a stage left open. A
wording choice inside the compiler's own authority (how to order two clauses, how to phrase an assertion) is
decided and traceable to its field.
