# Grok

Use the capability registry as authority. The notes below are operator field observations, not capability
claims - the registry overrules any of them for a specific version.

## Where it is strong

- Strong Chinese comprehension, and it composes Chinese text in-frame better than the alternatives here.
- Effects work holds up.
- Adheres closely to the prompt. What is stated tends to be what is rendered, which makes it unusually
  responsive to explicit constraints - and unusually punishing when a constraint is left implicit.

## Known failure modes

- **Direct gaze on the final frame.** This model has a strong default bias toward the subject looking into
  the lens at the end of a shot. When `end_frame_direct_gaze` is active, patch the final interval with the
  approved eyeline, the body orientation, a profile or rear three-quarter orientation, and an explicit
  no-camera-acknowledgment constraint. Treat this as expected behaviour to be suppressed, not an anomaly.
- **Do not impose an end-frame reference.** Constraining the last frame degrades the result here; leave it
  unconstrained unless the canonical shot and the exact version require otherwise.
- Multiple movement trajectories in one shot are not representable. One trajectory per shot.
- Chinese dialogue does not reliably fit a 15-second bound. Keep dialogue duration within the version's
  declared limits and split when it does not fit.
- TTS can produce audible noise; character animation can be inconsistent across a long take.

## Check the output for

Direct gaze on the final frame, profile loss, identity drift, dialogue noise, unintended pose reset.
