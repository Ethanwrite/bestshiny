---
name: model-prompting
description: Adapt an approved canonical shot specification into concise prompt language for the model that Model Router already selected, without changing creative facts or constructing provider payload fields. Use after routing for Kling, Veo, Seedance, Grok or Wan video generation or GPT Image 2 image generation, or when reviewing whether a model-specific prompt preserved action, identity, blocking, eyelines, continuity and supported controls.
metadata:
  category: model-prompting
---

# Model Prompting

## Position in the pipeline

After Model Router has chosen the model, before the adapter builds the request.

```text
Model Router (chooses) -> [this Skill] (phrases) -> Adapter (maps to API fields)
```

The selection is already made and is not reopened here; the payload is not yet built and is not anticipated
here. This stage only changes how an approved shot is worded so that one specific model executes it well.

Phrasing is the entire remit. If an adaptation changes what the frame contains, it has stopped adapting and
started re-directing.

## Adapt

1. **Read the capability-registry entry for the exact model version.** The registry is the authority on what the
   model supports. Bundled reference notes are prior observations that age as models change; treat them as
   hypotheses the registry can overrule.
2. **Read only the matching reference** in `references/` - `kling.md`, `veo.md`, `seedance.md`, `grok.md`,
   `wan.md` for video, or `gpt-image.md` for image generation.
3. **Preserve every fact.** Subject identities, asset bindings, `dominant_action`, blocking, gaze targets, start
   state, end state, camera plan, lighting, dialogue, continuity requirements, locked style.
4. **Drop unsupported controls and say so.** When the registry marks a control unsupported, remove it and report
   the conflict. Simulating it in prose produces a prompt that reads as if the control applied and output that
   silently ignored it - the worst combination for diagnosis.
5. **Apply a failure patch only when this shot activates that failure prior.** A model's known weakness on fast
   rotation is irrelevant to a locked-off frame, and a patch that is always applied stops carrying information.
6. **Keep positive and negative instructions separate from transport concerns.**

## Boundaries

- The visual stages decide what the shot communicates. This stage does not improve their decisions.
- Model Router decides which model runs. Never re-rank, substitute or second-guess the selection.
- The adapter owns field names, reference slots, duration, resolution, audio flags and payload shape. Never emit
  them here.
- One dominant action and one dominant camera movement survive adaptation intact.
- Every gaze stays on its explicit target; camera acknowledgment stays forbidden unless it was approved.
- Record what was done: model version, registry version, prompt version, applied failure patches and omitted
  unsupported controls. An adaptation nobody can reconstruct cannot be evaluated later.

## Output

Return `model_id`, `model_version`, `positive_prompt`, `negative_prompt`, `applied_failure_patches`,
`unsupported_requirements`, `preserved_constraints` and `adapter_handoff`.
