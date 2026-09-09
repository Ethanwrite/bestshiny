---
name: cinematography
description: Design HOW WE SEE one approved shot - shot size, framing, composition, camera angle, height, position and one dominant movement, lens intent, depth of field, focus, motivated lighting, contrast, colour temperature, exposure intent and atmosphere - without changing what happens in it. Use for cinematography design after shot planning and before prompt compilation. Lighting is a sub-domain of this stage. Do not use to change plot, action, dialogue, characters, product facts or the ending, or to choose a model or provider.
metadata:
  category: cinematography
  role: cinematography
  stage: CINEMATOGRAPHY
  runtime: model
  model_role: CINEMATOGRAPHY_REASONING
  operations: [cinematography_design]
  authority:
    - shot_size
    - framing
    - composition
    - camera_angle
    - camera_height
    - camera_position
    - camera_movement
    - lens_intent
    - depth_of_field
    - focus
    - lighting
    - contrast
    - color_temperature
    - exposure_intent
    - atmosphere
    - subject_screen_position
  forbidden_authority:
    - plot
    - action
    - dialogue
    - characters
    - product_facts
    - ending
    - start_state
    - end_state
    - shot_boundaries
    - continuity_verdict
    - model
    - provider
  output_contract: CinematographyPlan
  escalates_to: shot_planner
  sub_domains: [lighting, camera-movement, composition]
---

# Cinematography

## Purpose

HOW WE SEE an approved moment. Story facts arrive fixed - the action, the line, the states, the gaze target,
the cast and its canonical assets - and this stage owns the photographic treatment of them: framing, angle,
height, position, one movement, lens behaviour, depth of field, focus, light, contrast, colour temperature,
exposure intent and atmosphere. Lighting, camera movement and composition are sub-domains here, informed by
their reference Skills; they are not separate agents.

## Pipeline Position

After the action is planned, before the continuity review and the compilation.

```text
Shot Planner (what happens) -> [Cinematography: how it is seen] -> Continuity -> Prompt Compiler
```

Runtime binding: `cinematography_design` resolves to this Skill under the `CINEMATOGRAPHY_REASONING` model
role, once per approved shot. The plan is recorded on the shot and read by the Prompt Compiler between the
timeline state and any explicit override; a shot with no plan compiles with the deterministic defaults and
the record says the stage fell back.

## Inputs

- The approved shot: dominant action, staging description, the line (if any), start state, end state, gaze
  target, present and identity-critical characters, duration, aspect ratio, prohibitions.
- Context: location, time, weather, atmosphere, the scene description, inherited continuity state and the
  previous shot's plan where one exists.
- Canonical bindings by reference only: character identity versions, product and prop asset ids, the locked
  style. Never their re-description.
- The reference Skills `lighting`, `camera-movement` and `composition` as method, not as authority.

## Authority

- Shot size and framing; subject screen positions; visual hierarchy and negative space.
- Camera angle, height, position and exactly one dominant movement with its start, path, speed and end.
- Lens intent (spatial exaggeration, natural perspective, compression, macro detail, facial-proportion
  preservation), depth of field and focus behaviour.
- Motivated lighting: source, direction, size, hardness, fill, rim, background separation, practicals,
  contrast, colour temperature relationships, exposure priority, material response.
- Atmosphere and the start / end composition as arrangements.
- Continuity checks the plan itself must pass (axis, eyelines, light direction across connected shots).

## Forbidden Authority

- The plot, the dominant action, the staging, the line, the characters, product facts, claims, copy or the
  ending. If `dominant_action` contains two actions, stop: that is a decomposition problem returned to the
  Shot Planner, not a coverage problem solved here.
- Shot boundaries, start state, end state or gaze target (Shot Planner). The gaze target is honoured, never
  reassigned; defaulting a gaze to the lens is a decision nobody made.
- The continuity verdict between shots (Continuity).
- Which model renders the shot or through which provider; no provider-specific payload, protocol mapping or
  vendor syntax. A plan carrying any of these keys is refused as a whole.

## Invariants

- Character, product, environment, prop, wardrobe and continuity bindings are invariants here; cinematography
  changes how an approved moment is seen, never what happens in it.
- One visible action, one dominant camera movement.
- Every gaze lands on the named person, object or off-screen location the plan received.
- Screen direction, eyelines and the established axis stay coherent with connected shots; light direction is
  continuity, not styling.
- In commercial work, product shape, label, logo, material response and readable hierarchy survive the
  treatment.
- Concrete blocking, distance, light and material language only. "Cinematic", camera brands and resolution
  slogans occupy space without constraining anything.

## Decision Rules

Order matters. Deciding light before framing, or framing before knowing the action, produces decisions that
have to be undone.

1. **Subject and approved action.** State `dominant_action` as given.
2. **Context.** Location, time, weather, atmosphere, inherited continuity state.
3. **Framing.** Shot size, camera angle, camera height, each subject's screen position, the approved gaze
   target for every subject.
4. **Lens behaviour from intent.** Derive it from what the frame must communicate; never assign a focal length
   because a genre is associated with one.
5. **One camera movement.** For video, exactly one dominant move with its start, path, speed and end; locked
   is a choice, not the absence of one.
6. **Motivated light.** Direction, softness, contrast, colour temperature, subject-background separation, and
   the source in the scene that justifies them; material dictates the plan.
7. **Selective refinement.** Depth of field, focus transitions, palette and texture only where they materially
   serve the approved intent.
8. **Validate.** The move is physically possible with the chosen framing and lens; axis, eyelines and light
   direction cohere with connected shots; the plan names no story fact it did not receive.

## Escalation Rules

- Two actions in `dominant_action`, a missing gaze target, or a state the action cannot reach: return to the
  Shot Planner as unresolved; do not repair the shot here.
- A required framing that would hide a required product, label or copy: report the conflict as unresolved
  rather than choosing.
- A continuity constraint the plan cannot satisfy (an axis it would have to cross, a light direction it
  cannot keep): name it in `continuity_checks` and `unresolved` for the Continuity stage.

## Output Contract

One `CinematographyPlan` object: `camera` (`framing`, `angle`, `height`, `position`, `dominant_movement`,
`speed`, `path`, `focus`, `screen_axis`, `lens_intent`, `depth_of_field`), `lighting` (`direction`,
`quality`, `contrast`, `color_temperature`, `practicals`, `motivation`, `exposure_intent`),
`subject_positions`, `atmosphere`, `start_composition`, `end_composition`, `continuity_checks` and
`unresolved`. Nothing else: an action, a line, a character fact, a product fact, an ending, a model or a
provider in the output refuses the whole plan.

## Unresolved Policy

If a field is unknown and photographic - a height, a lens intent, a fill ratio - decide it from the
approved intent and say why in the plan. If it depends on an invariant the plan did not receive - a canonical
look, a product's true colour, a required text, an identity - leave it unresolved and reference the asset
rather than describing it. Never invent a story fact, a state or a gaze to make a frame work.
