# GPT Image 2

`openai/gpt-image-2` on OpenRouter is the project's image model - the `IMAGE_GENERATION` primary. Use the
capability registry as authority. The notes below are operator field observations, not capability claims.

Unlike every other file here, this one describes an **image** model. A still frame has no dominant camera
movement, no temporal beat and no end state, so the parts of a canonical shot spec that describe change over
time have nothing to adapt into. Carry identity, blocking, framing, lighting and locked style; drop movement.

## Execution envelope

| Control | Envelope |
| --- | --- |
| Images per request | 1-10 |
| Reference images | 0-16 |
| Aspect ratio | `1:1`, `3:2`, `2:3`, `4:3`, `3:4`, `16:9`, `9:16`, `21:9`, `auto` |
| Quality | `auto`, `low`, `medium`, `high` |
| Background | `auto`, `opaque` - there is no transparent background on this model |
| Context | 400K |

The adapter rejects a request outside this envelope before it is submitted, so an over-large batch or a
seventeenth reference is a local error, not a billed refusal.

## Where it is strong

- **Text inside the frame.** Signage, packaging copy, captions and titles hold up better here than on the
  video models, which is what makes it the right choice for a commercial or product still.
- **Editing an existing image.** Reference images are first-class: it will preserve a character or a product
  across a change of season, lighting or background rather than regenerating something similar.
- Sustained identity across a reference set, which is what a canonical character frame needs.

## Known failure modes

- **Under-specified edits drift.** With references present but no explicit statement of what must not change,
  it treats more of the frame as editable than intended. Name the invariants - face, wardrobe, product
  geometry, logo - before naming the change.
- **A batch is variety, not iteration.** Asking for `n` images returns `n` independent attempts at the same
  prompt; it does not refine across them. Use a batch to choose between compositions, not to converge.
- Long prose descriptions of a *sequence* get compressed into one composite frame. A still shows one moment;
  state which moment.

## How to prompt it

- One moment, stated as a moment: subject, position in frame, what they are doing at that instant.
- Put identity and locked-style invariants first, editable variables after, and mark which is which.
- Name the reference role of each input image when more than one is supplied - character, product, style,
  background - because the model cannot infer which one is authoritative for what.
- Ask for the aspect ratio the shot declares. `auto` lets the model choose and it will not match the project.

## Check the output for

Identity drift against the reference, altered product geometry or logo, text that is misspelled or in the
wrong language, an aspect ratio that does not match the shot, style drift away from the locked style.
