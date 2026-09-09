---
name: composition
description: Method reference for arranging visual hierarchy, subject and prop placement, depth layers, eyeline geometry and negative space inside one approved still or keyframe, with a mobile-safe crop check. Consulted by the Cinematography stage; not a separate runtime agent. Never changes story action, camera movement, lens behaviour, lighting or cross-shot continuity.
metadata:
  category: composition
  role: composition_reference
  stage: CINEMATOGRAPHY
  runtime: reference
  bound_to: cinematography
  authority:
    - visual_hierarchy
    - subject_screen_position
    - depth_layers
    - eyeline_geometry
    - negative_space
    - required_visibility
    - mobile_crop_check
  forbidden_authority:
    - action
    - dialogue
    - gaze_target
    - camera_movement
    - lens
    - lighting
    - continuity_verdict
    - asset_substitution
    - model
    - provider
  output_contract: subject_positions, start_composition and end_composition of CinematographyPlan
  escalates_to: cinematography
---

# Composition

## Purpose

Where things sit inside one frame and what the eye reads first. Composition owns the layout of an approved
moment; it does not own what happens, how the camera behaves, or how the frame is lit. The narrow scope is
deliberate: composition problems and continuity problems look identical from inside one frame, and a
composition fix applied to a continuity break makes the break harder to find.

## Pipeline Position

A sub-domain of the Cinematography stage, applied after the action and the camera are decided; reference
method for the `cinematography` Skill's `subject_positions`, `start_composition` and `end_composition`. No
runtime operation of its own.

## Inputs

The approved action, the cast in frame, the approved gaze targets, the required props, product and copy, the
framing and lens already chosen, the delivery aspect ratio and platform.

## Authority

The single primary focus and the priority order of supporting elements; each subject's and prop's screen
position and depth layer; eyeline geometry that resolves spatially; negative space reserved for a stated
purpose; required visibility of story-critical props, product silhouette, label, logo and text; the
delivery-crop check.

## Forbidden Authority

Adding, removing or reordering story actions; choosing a lens, camera path, lighting plan or a new gaze
target; resolving a cross-shot axis or state conflict (that is a continuity verdict and needs both frames);
swapping an approved asset to improve balance; the model or provider.

## Invariants

- A frame with two primary focuses has none, and the renderer will pick one arbitrarily.
- The approved gaze target must be somewhere the subject could actually be looking; an eyeline that does not
  resolve spatially reads as vacancy. Never redirect a gaze to the lens.
- Empty space without a purpose is an unfinished frame, not a minimal one.
- A better-balanced frame containing the wrong product is worse than an awkward frame containing the right
  one.

## Decision Rules

1. **Name the single primary focus**, then supporting elements in priority order.
2. **Map every required subject and prop** to screen left, centre or right, and to foreground, midground or
   background; state scale and overlap only where they change what is readable.
3. **Make the eyeline geometric** against where both subject and target sit.
4. **Reserve negative space for a stated purpose** - copy, movement room, environmental context.
5. **Protect required visibility** of story-critical props, product silhouette, label, logo and required text.
6. **Check the delivery crop.** At the target aspect ratio and at phone size, the primary action and any
   required text must survive crop edges, overlays and visual noise; detail that needs a pause has already
   failed.
7. **Treat start and end keyframes independently**, then report any handoff risk rather than resolving it.

## Escalation Rules

A layout that cannot keep a required element visible at the delivery crop, or an eyeline that cannot resolve
against the approved gaze target, is reported to Cinematography as unresolved; a cross-shot conflict is
reported for the Continuity stage.

## Output Contract

The `subject_positions`, `start_composition`, `end_composition` and layout-related `continuity_checks` of
the `CinematographyPlan`.

## Unresolved Policy

Decide free layout variables from the approved intent. Never move a required asset out of frame, never
substitute one, and never invent a subject or prop to fill space.
