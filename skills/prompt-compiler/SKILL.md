---
name: prompt-compiler
description: Convert an approved video shot specification into concise internal video-generation instructions. Use only in Autopilot after story, action, cinematography, asset references, and continuity are approved; do not use for user-visible image prompt correction.
---

# Prompt Compiler

Translate approved specifications into generation instructions. Preserve decisions; do not add story actions or redesign characters.

## Required input

Require the approved dominant action, start state, end state, duration, aspect ratio, subject position, gaze target, shot size, camera angle, lighting, one camera movement, environment, props, identity references, and continuity references. Return a missing-field error when a required value cannot be safely inferred.

## Compile

1. Put immutable identity, wardrobe, environment, prop, and spatial facts first.
2. State the start composition and every subject's gaze target.
3. Describe exactly one dominant action and one dominant camera movement.
4. State the end composition without introducing another action.
5. Bind asset IDs separately from prose so the gateway can reuse approved provider media IDs.
6. Add concise negative constraints for identity drift, duplicate people, extra limbs, prop changes, unintended cuts, text artifacts, and looking into the lens.
7. Return continuity assertions that QC can compare with the previous shot end state and approved references.

## Model adaptation

- For Google Flow/Veo, use concise spatial language, explicit start/end positions, and a single physically possible trajectory.
- For Omni Flash, keep instructions short and literal; never compress multiple movements into one shot.
- For Grok video, preserve its strong Chinese comprehension but explicitly forbid looking into the lens. Do not force an ending reference frame unless the approved workflow requires it.
- For Wan or Seedance, split long narrative action before compiling. Use Seedance for approved highlight shots, not as permission to combine actions.
- Keep provider-private fields outside the creative prompt; the provider adapter owns protocol mapping.

## Output contract

Return `positive_prompt`, `negative_prompt`, `asset_bindings`, `continuity_assertions`, `provider`, `model`, and `qc_checklist`. If compilation would change an approved fact, return `REQUIRES_DIRECTOR_REVIEW` instead.
