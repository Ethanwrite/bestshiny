---
name: composition
description: Arrange visual hierarchy, subject and prop placement, depth layers, eyeline geometry, and negative space inside an approved still or keyframe. Use when a shot needs a readable frame layout, mobile-safe hierarchy, product visibility, or composition repair without changing story action, camera movement, lens behavior, lighting, or cross-shot continuity.
---

# Composition

Own spatial hierarchy inside the frame. Preserve the approved action, identity versions, environment, props,
gaze targets, and product facts.

## Arrange the frame

1. Name the single primary visual focus and any supporting elements in priority order.
2. Map each required subject and prop to screen left, center, or right and to foreground, midground, or
   background. State scale and overlap only when they affect readability.
3. Keep the approved gaze target explicit and make the eyeline connect geometrically to that person, object,
   or off-screen location. Never redirect a gaze to the lens unless approved.
4. Reserve negative space only for an approved purpose such as copy, movement room, or environmental context.
5. Keep story-critical props, product shape, label, logo, and required text visible and unobstructed.
6. Check the target aspect ratio and mobile preview. Do not hide the primary action or required text near crop
   edges, overlays, or visually noisy areas.
7. When given separate start and end keyframes, describe each layout independently and report any handoff risk
   to the continuity skill.

## Boundaries

- Do not add, remove, or reorder story actions.
- Do not choose a lens, camera path, lighting plan, or new gaze target.
- Do not resolve cross-shot axis or state conflicts; report them for continuity review.
- Do not replace an approved asset to improve balance.

Return `visual_focus`, `hierarchy`, `frame_map`, `depth_layers`, `eyeline_geometry`, `negative_space`,
`required_visibility`, `mobile_crop_check`, and `handoff_risks`.
