---
name: image-prompt-corrector
description: Method reference for professionally rewriting a user-visible image generation prompt while preserving the requested subject, identity, product, wardrobe, scene and edit scope, for portrait, beauty and fashion, product, commercial, scene-concept or reference-character prompts. The platform's deterministic corrector and prompt-refiner roles implement it; it is user-facing, outside the production pipeline, and not a runtime-bound production Skill.
metadata:
  category: image-prompt-corrector
  role: image_prompt_corrector_reference
  stage: USER_TOOL
  runtime: reference
  bound_to: null
  authority:
    - task_detection
    - observable_visual_decisions
    - conflict_surfacing
    - change_reporting
  forbidden_authority:
    - identity
    - facial_geometry
    - product_shape
    - label
    - logo
    - core_background
    - pose
    - expression
    - camera
    - model
    - provider
  output_contract: original_prompt, corrected_prompt, detected_type, identity_preservation_mode, preserved_constraints, editable_variables, changes
  escalates_to: user
---

# Image Prompt Corrector

## Purpose

Enhance, do not redesign. The user owns this prompt and will see the result. The failure mode is specific and
seductive: a prompt with a weak subject invites a better subject; a plain outfit invites a nicer one. Every
such improvement returns something the user did not ask for and cannot easily detect, because it arrives
wrapped in more professional language.

## Pipeline Position

User-facing, outside the production pipeline. The deterministic `ImagePromptCorrector` and the platform's
prompt-refiner model roles implement this method under fact locks; the compilation record names this Skill's
version as consulted, never as injected.

## Inputs

The user's prompt in its source language, the detected task, any reference character or product, and the
user's explicit edit request.

## Authority

Detecting the task (`portrait`, `beauty_fashion`, `product`, `commercial`, `scene_concept`,
`reference_character_regeneration`); replacing vague quality words with observable decisions - composition,
light direction, contrast, material, texture, depth, palette, visual hierarchy; surfacing contradictions;
reporting every change plainly.

## Forbidden Authority

Never silently change identity, facial geometry, gender, skin tone, hairstyle, wardrobe, body, product shape,
label, logo, core background, pose, expression or camera; change any of them only on an explicit request.
Never choose a model or provider.

## Invariants

- In reference mode, identity invariants override every enhancement suggestion in every reference note.
- Never partially translate; return the corrected prompt in the user's source language and keep the original
  intact alongside it so the correction can be rejected.
- Never add empty prestige tokens - `masterpiece`, `best quality`, `8K`, camera brands, or a reflexive 85mm
  lens; they displace real constraints and give the impression that something was improved.

## Decision Rules

1. Detect the task.
2. Extract what is fixed: subject, action, explicitly requested attributes, environment, product facts,
   required text, stated prohibitions.
3. In reference mode, split identity invariants from editable variables; every variable the user did not
   name is invariant for this revision.
4. Replace vague quality words with observable decisions chosen for the detected task, using
   `references/camera.md` for framing and depth, `references/lighting.md` for light and material, and the
   task reference (`portrait.md`, `beauty.md`, `product.md`, `commercial.md`, `scene.md`).
5. Resolve contradictions without inventing; when two requests genuinely conflict, surface the conflict.
6. Return the corrected prompt with every factual difference listed in `changes`.

## Escalation Rules

A conflict the user must decide, or a requested change to an identity invariant, is surfaced to the user;
it is never picked silently.

## Output Contract

`original_prompt`, `corrected_prompt`, `detected_type`, `identity_preservation_mode`,
`preserved_constraints`, `editable_variables` and `changes`.

## Unresolved Policy

An ambiguity about the subject, the product, the identity or the scope of the edit is surfaced, not
resolved; only observable rendering decisions within the detected task are decided freely.
