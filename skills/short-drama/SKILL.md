---
name: short-drama
description: Convert an approved short-drama or vertical-commercial scene into executable shots, each with one dominant visible action, explicit start and end states, named gaze targets and a mobile-readable opening hook. Use after Director approval and before cinematography, for shot decomposition, pacing repair, action de-compression, or checking that the first seconds land on a phone screen.
metadata:
  category: short-drama
---

# Short Drama Shot Planning

## Position in the pipeline

Between an approved scene and its visual design. Story arrives approved; an ordered list of shots that a
generator can actually execute leaves. Preserve the Director's story facts, hook intent, emotional beat,
dialogue, ending and prohibitions - this stage decomposes them, it does not reimagine them.

## Decompose

1. **Identify the opening obligation.** What must the first one to three seconds make the viewer notice, ask or
   feel? Short-form is chosen against, not selected into: the opening beat competes with a thumb.
2. **Cut at visible state changes.** Each generation shot holds exactly one dominant action. Split concurrent
   turns, walks, prop interactions, reveals and reaction beats whenever they cannot stay stable together.
3. **Give each shot three concrete parts** - a start state, one visible action, an end state. The action must be
   observable. "Realises she was lied to" is an internal event; the shot needs the look, the stillness or the
   turn that shows it.
4. **Name every gaze target.** Another character, a prop, an off-screen location. Never leave it to default, and
   never default it to the camera.
5. **Give the action room.** Duration must let the action complete. Keep required dialogue in a shot only when
   mouth movement and the dominant action stay compatible; a line delivered mid-turn desynchronises both.
6. **Check the hook on a phone.** One obvious visual question or change, an identifiable subject or product,
   legible required text, no competing action in the critical beat.
7. **Chain the states.** Each end state becomes the next start state. Hand the list on without prescribing
   framing, lens, movement or light.

## Reject and split

- Multiple independent movement trajectories or simultaneous story beats in one shot. Compression is the single
  most reliable way to produce split subjects and deformed bodies.
- An opening that depends on small background detail, or on more subjects than a small screen can resolve.
- Any invented action, camera move, lighting choice or provider tactic.
- Unresolved story or hook questions - return them to Director rather than answering them here.

## Output

Return `approved_hook_intent`, `mobile_hook_check`, and an ordered shot list where each entry carries
`dominant_action`, `start_state`, `end_state`, `gaze_targets`, `duration`, `dialogue_constraint` and
`continuity_handoff`.
