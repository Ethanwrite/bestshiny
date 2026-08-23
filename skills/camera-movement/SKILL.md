---
name: camera-movement
description: Specify exactly one physically plausible camera movement with an explicit start, path, speed, subject relationship and end. Use when an approved video shot needs movement design, trajectory repair, screen-axis protection, or a check for conflicting or impossible camera instructions.
metadata:
  category: camera-movement
---

# Camera Movement

## Position in the pipeline

One approved action, one camera. This stage turns a movement intention into a trajectory a renderer can execute
without inventing a second shot.

Choose exactly one dominant move: locked, pan, tilt, dolly, truck, crane, orbit or handheld follow. Locked is a
choice, not the absence of one - a still camera on a moving subject is a deliberate and often stronger decision
than motion for its own sake.

## Why one

Video models resolve a single continuous trajectory well and blend competing trajectories badly. Two independent
moves in one instruction do not compose; they produce a drifting, unmotivated path that reads as an accidental
edit. Multiple moves are multiple shots.

## Specify

1. **Lock the approved action and starting blocking.** The camera answers to the action, not the reverse.
2. **Name the start.** Camera position, height, angle and distance from the subject.
3. **Name the path relative to something real** - the subject or the environment. A movement label alone
   ("dolly in") is not a path; it omits from where, past what, and how far.
4. **Name the speed** and whether it is constant, eases in or eases out. Constant speed on a subject that
   accelerates reads as a mismatch.
5. **Name the end framing and subject orientation** without introducing another action. The end of a move is a
   composition, not an event.
6. **Preserve** screen direction, the gaze target, physical obstacles and the established axis.

## Reject

- Combined independent trajectories - push-in plus orbit plus crane. Split them into separate shots.
- A label used for the wrong motion: pan is rotation, not translation; dolly is translation, not rotation.
  Confusing them produces a move that contradicts the framing it claims to reach.
- Crossing the action axis unless the shot explicitly motivates and reveals the crossing. An unmotivated cross
  flips screen direction and breaks the next handoff.
- The subject acknowledging the camera at the end of a move, unless that gaze was approved.
- Impossible acceleration, collision, or a path through solid geometry.

## Output

Return `movement`, `start_camera_state`, `path`, `speed`, `subject_relationship`, `end_camera_state` and
`continuity_constraints`.
