---
name: image-prompt-corrector
description: Professionally rewrite user-visible image generation prompts while preserving the requested subject, identity, product, wardrobe, scene, and edit scope. Use for portrait, beauty/fashion, product, commercial, scene-concept, or reference-character image prompts when the user asks to enhance, correct, polish, or professionally rewrite a prompt without redesigning it.
---

# Image Prompt Corrector

Apply one rule above all others: enhance, do not redesign.

## Workflow

1. Detect `portrait`, `beauty_fashion`, `product`, `commercial`, `scene_concept`, or
   `reference_character_regeneration`.
2. Extract the subject, action, explicitly requested attributes, environment, product facts, text, and
   prohibited changes.
3. In reference mode, split identity invariants from explicitly editable variables. Treat every
   unrequested variable as invariant for this revision.
4. Replace vague quality words with observable composition, light, contrast, material, texture, depth,
   palette, and visual hierarchy choices appropriate to the task.
5. Remove contradictions without inventing a new subject, product, outfit, scene, pose, expression, or
   camera request.
6. Return the corrected prompt in the user's source language, plus preserved constraints and a concise
   change list. Never partially translate a Chinese prompt. Preserve the original prompt separately so
   the user can undo.

## Knowledge routing

- Read `references/camera.md` only when framing, perspective, distance, or depth needs clarification.
- Read `references/lighting.md` whenever adding or resolving light direction and material response.
- Read the task reference matching the detected type: `portrait.md`, `beauty.md`, `product.md`,
  `commercial.md`, or `scene.md`.
- In reference-character mode, identity invariants override all visual enhancement suggestions.

## Invariants

Never silently change identity, facial geometry, gender, skin tone, hairstyle, wardrobe, body, product
shape, label, logo, core background, pose, expression, or camera. Change a variable only when the user
explicitly requests it. Do not add empty prestige tokens such as `masterpiece`, `best quality`, `8K`,
camera brands, or a default 85mm lens.

## Output

Return `original_prompt`, `corrected_prompt`, `detected_type`, `identity_preservation_mode`,
`preserved_constraints`, `editable_variables`, and `changes`. Never hide a factual change inside style
language.
