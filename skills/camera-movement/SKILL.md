---
name: camera-movement
description: Specify one physically plausible camera movement with a clear start frame, path, speed, subject relationship, and end frame. Use when an approved video shot needs movement design, trajectory repair, screen-axis protection, or a check for conflicting camera instructions.
---

# Camera Movement

Choose exactly one dominant move: locked, pan, tilt, dolly, truck, crane, orbit, or handheld follow. A locked
camera is a deliberate movement choice.

## Specify

1. Lock the approved subject action and starting blocking.
2. Name camera start position, height, angle, and distance.
3. Name the path relative to the subject or environment, not only a movement label.
4. Name speed and whether it remains constant, eases in, or eases out.
5. Name the end framing and subject orientation without introducing another action.
6. Preserve screen direction, gaze target, obstacles, and the established camera axis.

## Reject conflicts

- Do not combine independent trajectories such as push-in plus orbit plus crane.
- Do not use pan for physical translation or dolly for rotation.
- Do not cross the action axis unless the approved shot explicitly motivates and reveals the crossing.
- Do not make the subject acknowledge the camera at the end.
- Do not imply impossible acceleration, collision, or a path through solid geometry.

Return `movement`, `start_camera_state`, `path`, `speed`, `subject_relationship`, `end_camera_state`, and
`continuity_constraints`.
