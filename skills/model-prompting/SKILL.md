---
name: model-prompting
description: Method reference for adapting an approved, compiled prompt package into concise prompt language for the model that Model Router already selected, without changing creative facts or constructing provider payload fields. Consulted at the adapter stage for Kling, Veo, Seedance, Grok or Wan video generation and GPT Image image generation; it has no runtime call site of its own and never selects a model.
metadata:
  category: model-prompting
  role: model_prompting_reference
  stage: ADAPTATION
  runtime: reference
  bound_to: null
  authority:
    - model_specific_phrasing
    - unsupported_control_reporting
    - failure_patch_activation
    - adaptation_record
  forbidden_authority:
    - creative_facts
    - model_selection
    - provider_selection
    - payload_fields
    - action
    - dialogue
    - framing
    - lighting
  output_contract: model_id, model_version, positive_prompt, negative_prompt, applied_failure_patches, unsupported_requirements, preserved_constraints, adapter_handoff
  escalates_to: prompt_compiler
---

# Model Prompting

## Purpose

Change how an approved, compiled shot is worded so that one specific model executes it well, and nothing
else. Phrasing is the entire remit: if an adaptation changes what the frame contains, it has stopped
adapting and started re-directing.

## Pipeline Position

```text
Model Router (chooses) -> [this reference] (phrases) -> Adapter (maps to API fields)
```

After Model Router has chosen the model, before the adapter builds the request. The selection is already
made and is not reopened here; the payload is not yet built and is not anticipated here. This Skill is
reference method for the platform's model adapters; it is not runtime-bound and is never injected alone.

## Inputs

The compiled `PromptCompilerOutput`, the selected model's exact version and its capability-registry entry
(the authority on what the model supports), and the matching reference note in `references/` - `kling.md`,
`veo.md`, `seedance.md`, `grok.md`, `wan.md` for video, or `gpt-image.md` for image generation.

## Authority

Model-specific phrasing of the positive and negative prompt; dropping unsupported controls and reporting
them; applying a failure patch when this shot activates that failure prior; recording what was done.

## Forbidden Authority

Every creative fact - subject identities, asset bindings, `dominant_action`, blocking, gaze targets, start
state, end state, camera plan, lighting, dialogue, continuity requirements, locked style; the model choice
(never re-rank, substitute or second-guess); the adapter's field names, reference slots, duration,
resolution, audio flags and payload shape.

## Invariants

- The registry is the authority on what the model supports; bundled reference notes are prior observations
  that age as models change and are hypotheses the registry can overrule.
- One dominant action and one dominant camera movement survive adaptation intact.
- Every gaze stays on its explicit target; camera acknowledgment stays forbidden unless it was approved.
- A control the registry marks unsupported is removed and reported, never simulated in prose: a prompt that
  reads as if the control applied, with output that silently ignored it, is the worst combination for
  diagnosis.

## Decision Rules

1. Read the capability-registry entry for the exact model version.
2. Read only the matching reference note.
3. Preserve every fact.
4. Drop unsupported controls and say so.
5. Apply a failure patch only when this shot activates that failure prior; a patch that is always applied
   stops carrying information.
6. Keep positive and negative instructions separate from transport concerns.
7. Record model version, registry version, prompt version, applied failure patches and omitted unsupported
   controls; an adaptation nobody can reconstruct cannot be evaluated later.

## Escalation Rules

A requirement the selected model cannot honour is reported in `unsupported_requirements` for the router's
observation ledger; it is never hidden, and the model is never swapped here.

## Output Contract

`model_id`, `model_version`, `positive_prompt`, `negative_prompt`, `applied_failure_patches`,
`unsupported_requirements`, `preserved_constraints` and `adapter_handoff`.

## Unresolved Policy

An adaptation never resolves a creative field the package left unresolved; a package that is not
`COMPILED` is not adapted.
