---
name: cinematography
description: Design production-ready framing, lens behaviour, subject placement, eyelines, lighting and camera movement for one approved shot without changing its story action. Use when a scene or shot needs photographic language, visual blocking, lens and perspective decisions, or a cinematography quality check before prompt compilation.
metadata:
  category: cinematography
---

# Cinematography

## Position in the pipeline

After the action is approved and decomposed, before the specification is compiled. Story facts arrive fixed;
the photographic treatment of them is the variable this stage owns.

Character, product, environment, prop, wardrobe and continuity bindings are invariants here. Cinematography
changes how an approved moment is seen, never what happens in it.

## Build the shot

Order matters. Deciding light before framing, or framing before knowing the action, produces decisions that
have to be undone.

1. **Subject and approved action.** State `dominant_action` as given. If it contains two actions, stop: that is
   a decomposition problem, not a coverage problem.
2. **Context.** Location, time, weather, atmosphere, inherited continuity state.
3. **Framing.** Shot size, camera angle, camera height, each subject's screen position, and an explicit gaze
   target for every subject.
4. **Lens behaviour from intent.** Spatial exaggeration, natural perspective, compression, macro detail or
   facial-proportion preservation. Derive it from what the frame must communicate - never assign a focal length
   because a genre is associated with one.
5. **One camera movement.** For video, exactly one dominant move, with its start, path, speed and end.
6. **Motivated light.** Direction, softness, contrast, colour temperature, subject-background separation, and
   the source in the scene that justifies them.
7. **Selective refinement.** Depth of field, focus transitions, palette and texture only where they materially
   serve the approved intent.

## Validate

- The move must be physically possible and compatible with the chosen framing and lens behaviour. A push-in that
  the stated lens cannot produce is a contradiction the renderer will resolve arbitrarily.
- Screen direction, eyelines and the established axis stay coherent with connected shots.
- Every gaze lands on a named person, object or off-screen location. Defaulting to the lens is a decision nobody
  made.
- One visible action, one dominant camera movement.
- In commercial work, product shape, label, logo, material response and readable hierarchy survive the treatment.
- Concrete blocking, distance, light and material language only. `cinematic`, camera brands and resolution
  slogans occupy space without constraining anything.

## Output

Return framing, angle, height, subject positions, gaze targets, lens behaviour, focus behaviour, camera movement,
lighting, start composition, end composition, continuity checks and any unresolved constraint. Never emit a
provider-specific payload - protocol mapping belongs to the adapter, and a payload written here would encode a
model choice this stage does not make.
