---
name: character-consistency
description: Preserve a canonical character identity across image regeneration, edits, storyboards, and video shots while allowing only explicitly requested changes. Use when selecting identity anchors, binding reference views, regenerating a known person, checking drift, or carrying wardrobe and physical state through connected shots.
---

# Character Consistency

Treat canonical identity, temporal state, and editable variables as separate layers.

## Lock identity

1. Bind the approved identity version and reuse its content fingerprints and provider media IDs.
2. Preserve facial structure and proportions, eye geometry and spacing, nose and lip geometry, jawline, skin
   tone, hairline, recognizable asymmetry, and signature traits.
3. Use front, profile, three-quarter, full-body, and back/hairstyle references when the target view requires them.
4. Preserve body silhouette, age cues, and distinguishing details across all views.

## Apply an edit

1. List the exact variables the user requested to change.
2. Treat every unrequested variable as invariant for that revision, including expression, pose, camera,
   lighting, wardrobe, and background.
3. Create a new approved identity version when a canonical trait intentionally changes; never overwrite the
   previous version.

## Carry temporal state

- Preserve current wardrobe, hair state, makeup, injury, dirt, wetness, held props, screen position, body
  orientation, and gaze target when continuity requires them.
- Use the previous final frame as temporal evidence, not as authority over canonical identity.
- During occlusion, evaluate hair, body silhouette, costume, and tracking continuity; do not infer identity from
  face similarity alone.

Return `identity_version`, `canonical_references`, `identity_invariants`, `editable_variables`,
`temporal_state`, `reference_roles`, and `consistency_checks`.
