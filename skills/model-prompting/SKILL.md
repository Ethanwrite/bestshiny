---
name: model-prompting
description: Adapt an approved Canonical Shot Specification into concise model-family prompt language without changing creative facts or provider payload fields. Use after routing for Kling, Veo, Seedance, Grok, or Wan video generation, or when reviewing whether a model-specific prompt preserves action, identity, blocking, eyelines, continuity, and supported controls.
---

# Model Prompting

Express an approved shot for the selected model. Do not route models, invent capabilities, alter canonical facts,
or construct provider API payloads.

## Workflow

1. Read the current capability-registry entry for the exact model version. Treat bundled references as initial
   hypotheses, not permanent capability truth.
2. Read only the matching file in `references/`.
3. Preserve subject identities, asset bindings, dominant action, blocking, gaze targets, start state, end state,
   camera plan, lighting, dialogue, and continuity requirements.
4. Remove controls the registry marks unsupported and report the conflict; do not silently simulate them in
   prose.
5. Strengthen a known failure constraint only when the shot requirement activates that failure prior.
6. Return model-specific positive and negative instructions separately from transport fields.

## Boundaries

- Let the visual Skill decide what the shot should communicate.
- Let the model adapter decide field names, reference slots, duration, resolution, audio flags, and payload shape.
- Keep one dominant action and one dominant camera movement.
- Keep every character's gaze on an explicit target and forbid camera acknowledgment unless approved.
- Record the model version, registry version, prompt version, applied failure patches, and omitted unsupported
  controls.

## Output

Return `model_id`, `model_version`, `positive_prompt`, `negative_prompt`, `applied_failure_patches`,
`unsupported_requirements`, `preserved_constraints`, and `adapter_handoff`.
