---
name: camera-movement
description: Method reference for specifying exactly one physically plausible camera movement with an explicit start, path, speed, subject relationship and end. Consulted by the Cinematography stage for movement design, trajectory repair, screen-axis protection and checks for conflicting or impossible camera instructions; not a separate runtime agent.
metadata:
  category: camera-movement
  role: camera_movement_reference
  stage: CINEMATOGRAPHY
  runtime: reference
  bound_to: cinematography
  authority:
    - camera_movement
    - movement_start
    - movement_path
    - movement_speed
    - movement_end
    - screen_axis_protection
  forbidden_authority:
    - action
    - dialogue
    - shot_boundaries
    - gaze_target
    - lighting
    - continuity_verdict
    - model
    - provider
  output_contract: camera movement fields of CinematographyPlan
  escalates_to: cinematography
---

# Camera Movement

## Purpose

Turn a movement intention into a trajectory a renderer can execute without inventing a second shot. One
approved action, one camera: locked, pan, tilt, dolly, truck, crane, orbit or handheld follow. Locked is a
choice, not the absence of one - a still camera on a moving subject is a deliberate and often stronger
decision than motion for its own sake.

## Pipeline Position

A sub-domain of the Cinematography stage; reference method for the `cinematography` Skill's
`dominant_movement`, `speed`, `path` and `screen_axis` decisions. No runtime operation of its own.

## Inputs

The approved action and starting blocking, the framing and lens intent already chosen, the gaze target, the
established axis and the physical geometry of the scene.

## Authority

Exactly one dominant move: its start camera state, its path relative to something real, its speed and
easing, its end framing and subject orientation, and the axis it must protect.

## Forbidden Authority

The action itself, the shot boundaries, the gaze target, the light, the continuity verdict, the model or
provider. The camera answers to the action, not the reverse.

## Invariants

- Video models resolve a single continuous trajectory well and blend competing trajectories badly. Two
  independent moves in one instruction do not compose; multiple moves are multiple shots.
- Pan is rotation, not translation; dolly is translation, not rotation. A label used for the wrong motion
  produces a move that contradicts the framing it claims to reach.
- The end of a move is a composition, not an event.
- Screen direction, the gaze target, physical obstacles and the established axis are preserved.

## Decision Rules

1. **Lock the approved action and starting blocking.**
2. **Name the start.** Camera position, height, angle and distance from the subject.
3. **Name the path relative to something real** - the subject or the environment. "Dolly in" alone omits from
   where, past what, and how far.
4. **Name the speed** and whether it is constant, eases in or eases out; constant speed on a subject that
   accelerates reads as a mismatch.
5. **Name the end framing and subject orientation** without introducing another action.
6. **Reject** combined independent trajectories, an unmotivated axis cross, the subject acknowledging the
   camera at the end of a move unless that gaze was approved, impossible acceleration, collision, or a path
   through solid geometry.

## Escalation Rules

A move the approved framing and lens cannot produce, or one that would have to cross the axis to reach the
required end composition, is reported to Cinematography as unresolved; it is not executed arbitrarily.

## Output Contract

The movement fields of the `CinematographyPlan.camera` block: `dominant_movement`, `speed`, `path`,
`position` (start), `screen_axis`, and the end arrangement in `end_composition`.

## Unresolved Policy

Decide free movement variables from the action and the framing. Leave unresolved any move whose geometry
depends on a scene fact not in hand, and never invent a subject motion to motivate a camera move.
