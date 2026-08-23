---
name: image-prompt-corrector
description: Professionally rewrite a user-visible image generation prompt while preserving the requested subject, identity, product, wardrobe, scene and edit scope. Use for portrait, beauty and fashion, product, commercial, scene-concept or reference-character image prompts when the user asks to enhance, correct, polish or professionally rewrite a prompt rather than redesign it.
metadata:
  category: image-prompt-corrector
---

# Image Prompt Corrector

## Position in the pipeline

User-facing, and outside the production pipeline. The user owns this prompt and will see the result, so one rule
governs everything below: **enhance, do not redesign.**

The failure mode is specific and seductive. A prompt with a weak subject invites a better subject; a plain
outfit invites a nicer one. Every such improvement returns something the user did not ask for and cannot easily
detect, because it arrives wrapped in more professional language.

## Correct

1. **Detect the task**: `portrait`, `beauty_fashion`, `product`, `commercial`, `scene_concept` or
   `reference_character_regeneration`.
2. **Extract what is fixed**: subject, action, explicitly requested attributes, environment, product facts,
   required text, stated prohibitions.
3. **In reference mode, split identity invariants from editable variables.** Every variable the user did not
   name is invariant for this revision.
4. **Replace vague quality words with observable decisions** - composition, light direction, contrast, material,
   texture, depth, palette, visual hierarchy - chosen for the detected task. This is the actual work: `beautiful
   lighting` constrains nothing, while `low side key, hard shadow falloff, dark background separation` does.
5. **Resolve contradictions without inventing.** When two requests genuinely conflict, surface the conflict
   instead of silently picking a winner.
6. **Return the corrected prompt in the user's source language.** Never partially translate - a half-translated
   prompt is read inconsistently by the model and is unreadable by the user. Keep the original prompt intact
   alongside it so the correction can be rejected.

## Knowledge routing

- `references/camera.md` when framing, perspective, distance or depth needs clarifying.
- `references/lighting.md` whenever light direction or material response is added or resolved.
- The task reference for the detected type: `portrait.md`, `beauty.md`, `product.md`, `commercial.md` or
  `scene.md`.
- In reference-character mode, identity invariants override every enhancement suggestion in every reference.

## Invariants

Never silently change identity, facial geometry, gender, skin tone, hairstyle, wardrobe, body, product shape,
label, logo, core background, pose, expression or camera. Change any of them only on an explicit request.

Never add empty prestige tokens - `masterpiece`, `best quality`, `8K`, camera brands, or a reflexive 85mm lens.
They lengthen the prompt, displace real constraints, and give the user the impression that something was
improved.

## Output

Return `original_prompt`, `corrected_prompt`, `detected_type`, `identity_preservation_mode`,
`preserved_constraints`, `editable_variables` and `changes`. Every factual difference belongs in `changes`,
stated plainly - a change hidden inside style language is the one the user will discover too late.
