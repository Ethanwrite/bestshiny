---
name: composition
description: Arrange visual hierarchy, subject and prop placement, depth layers, eyeline geometry and negative space inside one approved still or keyframe. Use when a frame needs a readable layout, mobile-safe hierarchy, product visibility or composition repair - without changing story action, camera movement, lens behaviour, lighting or cross-shot continuity.
metadata:
  category: composition
---

# Composition

## Position in the pipeline

Inside a single frame, after the action and the camera are decided. This stage owns where things sit and what
the eye reads first. It does not own what happens, how the camera behaves, or how the frame is lit.

The narrow scope is deliberate. Composition problems and continuity problems look identical from inside one
frame, and a composition fix applied to a continuity break makes the break harder to find.

## Arrange the frame

1. **Name the single primary focus.** Then supporting elements in priority order. A frame with two primary
   focuses has none, and the renderer will pick one arbitrarily.
2. **Map every required subject and prop** to screen left, centre or right, and to foreground, midground or
   background. State scale and overlap only where they change what is readable.
3. **Make the eyeline geometric.** The approved gaze target must be somewhere the subject could actually be
   looking, given where both sit in the frame. An eyeline that does not resolve spatially reads as vacancy.
   Never redirect a gaze to the lens.
4. **Reserve negative space for a stated purpose** - copy, movement room, environmental context. Empty space
   without a purpose is an unfinished frame, not a minimal one.
5. **Protect required visibility.** Story-critical props, product silhouette, label, logo and required text stay
   unobstructed.
6. **Check the delivery crop.** At the target aspect ratio and at phone size, the primary action and any required
   text must survive crop edges, overlays and visual noise. Vertical short-form is watched on a small screen in
   motion; detail that needs a pause has already failed.
7. **Treat start and end keyframes independently.** Describe each layout on its own terms, then report any
   handoff risk rather than resolving it here.

## Boundaries

- Never add, remove or reorder story actions.
- Never choose a lens, camera path, lighting plan or new gaze target.
- Never resolve a cross-shot axis or state conflict; that is a continuity verdict, and it needs both frames.
- Never swap an approved asset to improve balance. A better-balanced frame containing the wrong product is worse
  than an awkward frame containing the right one.

## Output

Return `visual_focus`, `hierarchy`, `frame_map`, `depth_layers`, `eyeline_geometry`, `negative_space`,
`required_visibility`, `mobile_crop_check` and `handoff_risks`.
