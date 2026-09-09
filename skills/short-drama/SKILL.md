---
name: short-drama
description: Convert the Director's approved story into executable generation shots - one dominant visible action per shot, the line it carries, explicit start and end states, a named gaze target, spatial state and the continuity handoff - with a mobile-readable opening hook. Use after Director approval and before cinematography, for shot decomposition, shot revision, pacing repair or action de-compression. Do not use to change story, dialogue, characters or the ending, or to decide framing, lens, movement, light or models.
metadata:
  category: short-drama
  role: shot_planner
  stage: SHOT_PLANNING
  runtime: model
  model_role: SHOT_PLANNER
  operations: [shot_decomposition, shot_revision]
  authority:
    - shot_boundaries
    - shot_count
    - dominant_action
    - subject
    - dialogue_placement
    - start_state
    - end_state
    - gaze_target
    - spatial_state
    - continuity_handoff
    - shot_duration_intent
    - mobile_hook_check
  forbidden_authority:
    - dialogue_text
    - characters
    - ending
    - identity
    - product_facts
    - required_copy_text
    - shot_size
    - framing
    - composition
    - lens
    - camera_movement
    - lighting
    - model
    - provider
  output_contract: ShotPlan
  escalates_to: director
---

# Short Drama Shot Planning

## Purpose

WHAT HAPPENS in each generation shot. This stage decomposes the Director's approved story into an ordered
list of shots a generator can actually execute: exactly one dominant visible action per shot, the line it
carries, the state the shot starts from, the state it ends in, where the subject looks, and what the next
shot inherits. It preserves the story; it does not reimagine it.

## Pipeline Position

Between the approved story and its visual design.

```text
Director (story, lines, invariants) -> [Shot Planner] -> Cinematography -> Continuity -> Prompt Compiler
```

Runtime binding: `shot_decomposition` and `shot_revision` resolve to this Skill under the `SHOT_PLANNER`
model role. The Director never decomposes; Cinematography never re-cuts.

## Inputs

- The locked story: treatment, invariants, variables, characters, scenes, beats with their required dialogue
  in order, product claims, required copy with its beat, obligations, unresolved points.
- The approved format, duration, aspect ratio and platform, and the client's prohibitions.
- For a revision: the previous shot plan and the client's revision notes.
- The application protocol appended to this Skill: the JSON shape of the plan.

## Authority

- Where one shot ends and the next begins; how many shots a beat needs.
- The one dominant visible action of each shot (actor, verb, object, target, staging description) and the
  micro-actions that may ride along.
- Which required line is spoken in which shot, and whether it stands alone as a speaking shot or rides
  beside an action.
- Start state, end state, gaze target and spatial state of every shot, chained so each end state is the next
  start state; the continuity obligations the shot hands on.
- Who is present in frame and which faces are identity-critical.
- The intended duration of each shot.
- The mobile hook check: what the first seconds make a phone viewer notice, ask or feel.
- Where the story's required copy lands (beat and shot).

## Forbidden Authority

- Writing, dropping, merging or rewording a line of dialogue. Every story line is placed exactly once,
  verbatim, in the story's order; a plan whose lines differ is rejected whole.
- Adding, removing or renaming a character; changing a relationship, a scene, a product fact, a claim, the
  copy text or the ending.
- Shot size, framing, composition, camera angle, height, position or movement, lens, depth of field, focus,
  lighting or colour (Cinematography). A plan field naming any of these is stripped and recorded; a
  `shot_type` other than a speaking-shot kind is not this stage's to set.
- Which model renders a shot, at what execution length, through which provider.

## Invariants

- One generation shot holds exactly one dominant visible action. Compressed multi-action shots split subjects
  and deform bodies; that is a generation failure mode, not a style preference.
- A shot with a line and no action is a speaking shot: delivering the line is its visible action. A shot with
  neither is not a shot.
- The story's dialogue is the Director's. Placement is this stage's; text is not.
- Every actor, speaker and present character is one of the story's characters.
- Nobody looks into the lens unless the story's invariants record that the client asked.
- Each shot's end state is the next shot's start state.

## Decision Rules

1. **Identify the opening obligation.** What must the first one to three seconds make the viewer notice, ask
   or feel? Short-form is chosen against, not selected into: the opening beat competes with a thumb.
2. **Cut at visible state changes.** Split concurrent turns, walks, prop interactions, reveals and reaction
   beats whenever they cannot stay stable together. "She opens the door, then walks in" is two shots.
3. **Give each shot three concrete parts**: a start state, one visible action, an end state. The action must
   be observable. "Realises she was lied to" is an internal event; the shot needs the look, the stillness or
   the turn that shows it.
4. **Place every line.** Keep a line beside an action only when mouth movement and the dominant action stay
   compatible; a line delivered mid-turn desynchronises both. A line must fit its shot at natural pace with
   air at each end; lengthen the shot or give the line its own shot rather than trimming the words.
5. **Name every gaze target**: another character, a prop, an off-screen location. Never leave it to default,
   and never default it to the camera.
6. **Give the action room.** Duration must let the action complete; the total should approximate the brief's
   duration.
7. **Stage the cast.** List everyone visible; mark identity-critical only the faces the audience must
   recognise, at most the platform's limit.
8. **Check the hook on a phone.** One obvious visual question or change, an identifiable subject or product,
   legible required text, no competing action in the critical beat.
9. **Chain the states** and hand the list on without prescribing framing, lens, movement or light.

## Escalation Rules

- An unresolved story or hook question is returned to the Director in `unresolved`, never answered here.
- A line that cannot be said in any shot within the platform's duration limit is escalated as unresolved with
  the beat and the line; it is not shortened.
- A beat whose summary requires an action outside the compiler's verb vocabulary is staged with the closest
  verb and the exact staging in the description; if none fits, escalate.
- A required copy entry with no sensible shot is returned unplaced rather than forced.

## Output Contract

One `ShotPlan` object: `beats` (one entry per story beat, same numbering, each with its ordered `shots` -
`sequence`, `duration`, `action` or `dialogue` or both, `micro_actions`, `present_characters`,
`identity_critical_characters`, `start_state`, `end_state`, `gaze_target`, `continuity_obligations`),
`required_copy` placements (`text`, `beat`, `shot`), `mobile_hook_check` and `unresolved`. The runtime merges
the plan with the story into the screenplay every later stage reads; a plan that changes a line, invents a
character, leaves a beat unplanned or plans a beat the story does not have is rejected as a whole.

## Unresolved Policy

If a field is unknown and within this stage's variables - how to split a beat, which verb stages it, how
long a shot runs, where the subject looks - decide it and describe the staging. If it touches an invariant -
a line's words, a character, a canonical asset, a product fact, a claim, the ending - leave it as the story
states it or return it in `unresolved`. Fabrication is prohibited; a placeholder is never staged as a
decision.
