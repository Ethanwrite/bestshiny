---
name: short-drama
description: Convert an approved short-drama or vertical-commercial scene into executable shots with one dominant visible action, explicit start and end states, named gaze targets, and a mobile-readable opening hook. Use after Director approval and before cinematography when a scene needs shot decomposition, pacing repair, action de-compression, or a check that the first seconds communicate the approved hook on a phone screen.
---

# Short Drama Shot Planning

Own shot decomposition and short-form pacing. Preserve the Director's approved story facts, hook intent,
emotional beat, dialogue, ending, and prohibitions; do not replace the hook with a new creative idea.

## Build executable shots

1. Lock the approved scene action and identify what the opening one to three seconds must make the viewer
   notice, question, or feel.
2. Break the scene at visible state changes so each generation shot contains exactly one dominant action.
   Split concurrent turns, walks, prop interactions, reveals, and reaction beats when they cannot remain stable
   in one shot.
3. Give every shot a concrete start state, one visible action, and a concrete end state. The action must be
   observable rather than an internal intention.
4. Give every visible character a named gaze target: another character, a prop, or an off-screen location.
   Never default a character to looking into the camera.
5. Assign a duration that allows the action to complete without long-narrative compression. Keep required
   dialogue within a shot only when mouth movement and the dominant action remain compatible.
6. Check mobile-hook readability: one obvious visual question or change, an identifiable subject or product,
   legible required text, and no competing actions in the critical opening beat.
7. Carry each end state into the next start state and hand the resulting shot list to composition,
   cinematography, and continuity without prescribing their technical decisions.

## Reject and split

- Reject a shot containing multiple independent movement trajectories or simultaneous story beats.
- Reject an opening that depends on tiny background detail or too many subjects to read on a phone.
- Reject an invented action, camera move, lighting choice, or provider tactic.
- Mark unresolved story or hook choices for Director approval instead of guessing.

Return `approved_hook_intent`, `mobile_hook_check`, and an ordered shot list containing `dominant_action`,
`start_state`, `end_state`, `gaze_targets`, `duration`, `dialogue_constraint`, and `continuity_handoff`.
