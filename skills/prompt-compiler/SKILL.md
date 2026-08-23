---
name: prompt-compiler
description: Compile one already-approved CanonicalShotSpec into a provider-neutral prompt package - positive_prompt, negative_prompt, asset_bindings, continuity_assertions and qc_checklist. Use only after Director and policy approval, when a PromptCompilerInput envelope is ready to render. Do not use to invent story, to choose a generation model or Provider, to emit Provider API fields, or to correct a user-facing image prompt.
metadata:
  category: prompt-compiler
---

# Prompt Compiler

## Position in the pipeline

One stage, one job: an approved specification enters, a provider-neutral prompt package leaves.

```text
Director + policy approval -> PromptCompilerInput -> [this Skill] -> PromptCompilerOutput -> Model Router -> Adapter
```

Everything before the arrow is already decided. Everything after it belongs to another stage. Model Router
chooses which model renders the work; the Adapter maps the package onto that model's API. Neither decision may
be anticipated here, and no Provider or model name belongs anywhere in the output.

The boundary is what makes the output auditable. The moment this stage invents a missing beat, resolves an
ambiguity by guessing, or writes vendor syntax, downstream QC can no longer tell what was approved from what
was improvised.

## Input: the PromptCompilerInput envelope

Three top-level keys. The envelope is not the specification, and its keys are not specification fields.

| Key | Type | Meaning |
| --- | --- | --- |
| `shot_spec` | object | A complete CanonicalShotSpec. Every rendered fact comes from here. |
| `asset_bindings` | array of string | Canonical asset identifiers. Echo them; never invent, drop, reorder into meaning, or describe them. |
| `continuity_context` | object | `transition` plus `facts`. |

Inside `continuity_context`:

- `transition` is routing metadata - a label such as a cut, a time jump or a flashback. It explains why this
  frame follows the previous one. It is **not** permission to infer anything about what changed.
- `facts` is the only channel through which continuity claims may enter the output.

Inside `shot_spec`, the fields that carry rendered meaning are `intent`, `dominant_action`, `subjects` (each
with `screen_position`, `body_orientation`, `eyeline_target`, `pose`, `identity_constraints`), `props`,
`start_state`, `blocking`, `camera` (with `position`, `angle`, `framing`, `dominant_movement`, `speed`, `path`,
`focus`, `screen_axis`), `lighting`, `dialogue`, `end_state`, `continuity`, `style_lock`, `constraints`,
`allow_camera_gaze`, `duration`, `aspect_ratio` and `resolution`.

## Preflight

Before writing a single word of prompt, confirm the specification can actually be rendered:

1. `intent` and `dominant_action` are both present and non-empty.
2. `dominant_action` describes exactly one action. Two actions joined by "and then" are two shots.
3. `camera.dominant_movement` names exactly one movement.
4. Every entry in `subjects` has a non-empty `eyeline_target`.
5. `start_state` and `end_state` are both present, and the end state is reachable from the start state by the
   single named action alone.
6. `duration`, `aspect_ratio` and `resolution` are present.

If any check fails, stop. Do not compile a partial package and do not repair the specification yourself: name
the offending field paths in `missing_fields`, explain the blocker in `review_reason`, and return
`NOT_COMPILABLE`. Returning a blocker is a correct outcome, not a failure to perform.

## Compiling positive_prompt

Write continuous prose a renderer can follow, ordered so that immutable facts land before motion:

1. **Identity and canon first.** Named subjects with their `identity_constraints`, canonical wardrobe, canonical
   products and props, and the location. These anchor the frame and must appear before anything moves.
2. **Opening composition.** Each subject's `screen_position` and `body_orientation`, the arrangement from
   `blocking`, and every subject's `eyeline_target` stated explicitly.
3. **One action.** `dominant_action` and nothing else. No second beat, no reaction shot, no implied cut.
4. **One camera movement.** `camera.dominant_movement` with its `speed`, `path`, `framing`, `angle`, `focus`
   and `screen_axis`.
5. **Light.** Direction, quality, contrast and colour temperature, plus any practicals.
6. **Closing composition.** The `end_state` as an arrangement, never as a further action.
7. **Locked style.** When `style_lock` is populated, restate its constraints verbatim; palette, contrast,
   texture, rendering medium and edge treatment may not drift.

Rules that govern the prose:

- Every clause traces to a field. If you cannot point at the field, delete the clause.
- Preserve the specification's own wording for canonical nouns. Do not upgrade "red coat" into "crimson
  greatcoat" - a renamed fact is a changed fact.
- No adjective soup. "Cinematic", "masterpiece", "8K" and "highly detailed" describe nothing and displace
  content that does.
- When `allow_camera_gaze` is false, no subject acknowledges the lens. When it is true, honour the approved
  eyeline exactly, and do not also change `body_orientation` to face the camera.
- Write in the specification's `language`. Translating dialogue or canonical nouns changes them.
- Never mention this Skill, its status vocabulary, its checks, or any absent field inside the prompt text.

## Compiling negative_prompt

State what would falsify the approved work, drawn from what this specification actually risks - identity drift,
style or palette drift, altered canonical products or wardrobe, extra or duplicated subjects, duplicated limbs,
a second action, an unintended cut, text artefacts, and unapproved gaze into the lens when `allow_camera_gaze`
is false. Keep it to falsifiable failures. A negative that cannot be checked against the frame is decoration.

## Compiling asset_bindings

Echo the envelope's `asset_bindings` exactly, deduplicated, order preserved. Identifiers are references, not
description: they never appear inside the prompt text, and no identifier may be added that the envelope did not
supply.

## Compiling continuity_assertions

One assertion per entry in `continuity_context.facts`, phrased as a claim a reviewer can compare against the
previous frame and the approved references.

The `transition` label may shape how an assertion is worded, never what it claims. If a fact is not in `facts`,
it is not an assertion - however strongly the label implies it.

## Compiling qc_checklist

Verifiable checks over what this package actually asserts: the single action, the single camera movement, each
subject's identity binding and eyeline, the lighting, the end composition, the locked style, and the duration,
aspect ratio and resolution. Each entry must be answerable yes or no by looking at the rendered frames. Aesthetic
questions are not QC.

## Output contract

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

Two mutually exclusive shapes, both strictly enforced:

**`COMPILED`** carries both `positive_prompt` and `negative_prompt`, and leaves `missing_fields` empty and
`review_reason` null. A compiled package is a complete package.

**`NOT_COMPILABLE`** carries no `positive_prompt`, no `negative_prompt`, no `asset_bindings`, no
`continuity_assertions` and no `qc_checklist`, and must carry a `review_reason`. There is no partial success:
half a prompt is worse than none, because it looks finished.

No ninth field may be added. Routing keys, vendor parameter names, confidence scores, notes and commentary all
belong to other stages or to nobody.

## Out of scope

- **Story.** Never invent or extend plot, dialogue, motivation, or any state the specification does not contain.
- **Repair.** Never fill a gap with a plausible default. Report it and stop.
- **Selection.** Never name or presuppose which model or Provider renders the work.
- **Vendor syntax.** Never emit an API parameter, weight syntax, or any phrasing that only one model understands.
- **Leakage.** Never let these instructions, the envelope's key names, or reasoning about them reach the prompt
  text.
