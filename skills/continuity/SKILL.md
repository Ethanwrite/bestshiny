---
name: continuity
description: Review the state handoff between two connected shots - identity, wardrobe, props, geography, screen direction, gaze, body and object state, entrances and exits, lighting continuity, camera axis, canonical asset binding, previous end state against next start state - and return PASS, REPAIRABLE or ESCALATE with the smallest honest repair. Use for continuity review before generation and when diagnosing a visible break. Do not use to redesign framing, movement or light, to change story or dialogue, or to promote a frame to canon.
metadata:
  category: continuity
  role: continuity
  stage: CONTINUITY
  runtime: model
  model_role: CONTINUITY_REASONER
  operations: [continuity_review]
  authority:
    - identity_continuity
    - wardrobe_continuity
    - prop_continuity
    - geography_continuity
    - screen_direction
    - gaze_continuity
    - body_object_state
    - entrance_exit
    - lighting_continuity
    - camera_axis_continuity
    - canonical_asset_binding
    - end_start_state_contract
    - continuity_verdict
    - minimal_repair
  forbidden_authority:
    - framing
    - camera_movement
    - lighting_design
    - composition
    - action
    - dialogue
    - ending
    - identity_version_creation
    - canonical_promotion
    - model
    - provider
  output_contract: ContinuityReview
  escalates_to: director
---

# Continuity

## Purpose

Decide whether the contract between two connected shots holds. The previous shot's approved end state and
the next shot's start state form that contract; this stage says `PASS`, `REPAIRABLE` or `ESCALATE`, names
what matched and what did not, binds the evidence it used and proposes the smallest honest repair. A
discontinuity is an error only when nobody approved it; the job is to tell those cases apart and never to
quietly convert one into the other.

## Pipeline Position

Between any two connected shots, after cinematography and before compilation and generation.

```text
Shot n (approved end state, plan) -> [Continuity review] -> Shot n+1 (start state, plan) -> Prompt Compiler
```

Runtime binding: `continuity_review` resolves to this Skill under the `CONTINUITY_REASONER` model role, once
per adjacent pair. The verdict is recorded as a decision on the target shot; a pair the model could not
review carries the deterministic comparison and says so.

## Inputs

- The two shots' approved states (timeline state JSON: characters, positions, orientations, gaze targets,
  held props, props, location, time, lighting), their director intent (start / end state, gaze,
  obligations) and their cinematography plans.
- The declared transition (`CONTINUOUS`, `SCENE_CUT`, `LOCATION_CHANGE`, `TIME_JUMP`, `FLASH_FORWARD`,
  `MONTAGE`, `FLASHBACK`, `DREAM`, `EXPLICIT_RESET`) and its reconciliation status.
- Authoritative assets by reference: character identity versions, environment versions, promoted reference
  assets, registered `END_FRAME` evidence where it exists.
- The application protocol appended to this Skill: the JSON shape of the review.

## Authority

- Whether identity, wardrobe, hair and makeup state, injury, dirt, wetness, held props and which hand holds
  them, body orientation, screen side, direction of travel and gaze carry correctly.
- Whether environment topology, object positions, time, weather, motivated light direction, exposure family,
  camera axis and screen direction carry correctly.
- Whether the next action is reachable from the end state that was actually rendered.
- The verdict, the mismatch list with severity, the evidence used, the minimal repair, and whether approval is
  required.

## Forbidden Authority

- Redesigning framing, movement, lighting or composition: name the state they must preserve and return the
  decision to Cinematography.
- Changing an action, a line, a character fact or the ending: a missing pickup, turn or transfer is a missing
  shot returned to the Shot Planner, not a detail smoothed over.
- Inventing a transition, prop transfer, action or asset version to explain a mismatch away.
- Promoting a generated frame to canonical, creating an identity version, or overwriting an approved asset.
- Naming a model or provider. A review carrying any of these keys is refused as a whole.

## Invariants

| Transition | Effect on state |
| --- | --- |
| `CONTINUOUS` | Committed character, prop and costume state propagates. Every mismatch is a real mismatch. |
| `SCENE_CUT`, `LOCATION_CHANGE` | Spatial state resets. Identity and committed physical state still carry. |
| `TIME_JUMP`, `FLASH_FORWARD`, `MONTAGE` | State cannot simply carry; the handoff requires explicit reconciliation. |
| `FLASHBACK`, `DREAM` | A separate branch. Do not advance or contaminate the main timeline from inside it. |
| `EXPLICIT_RESET` | The contract is deliberately broken. Verify it was approved, then stop comparing. |

- A transition label explains why the next frame follows this one. It never licenses inventing what happened
  in between.
- Canonical assets are compared by binding, never by re-description; a prose description of a face is not
  evidence.
- Evidence rules are asymmetric: a confident mismatch fails; missing or low-confidence evidence goes to
  review. Absent evidence is never a pass.

## Decision Rules

1. **Resolve authority first.** Project, scene, shot order, character identity versions, environment versions
   and promoted reference assets. Comparing against the wrong version produces confident nonsense.
2. **Resolve the transition** before comparing anything; it determines which comparisons are meaningful.
3. **Compare the subject**, then **compare the world**, field by field, recording each mismatch with its
   previous and next value and a severity (`MINOR`, `MAJOR`, `IDENTITY`).
4. **Test reachability.** The next action must start from the end state that was actually rendered.
5. **Bind real evidence.** Use a registered `END_FRAME` image; a video asset id or a provider job id is not an
   end frame. Evidence that was never extracted cannot support a verdict.
6. **Classify.** `PASS` when nothing material moved or every move is explained by the transition;
   `REPAIRABLE` when a state can be restored without touching story action or canonical identity, and say the
   repair; `ESCALATE` when identity, geography or an unapproved jump is involved, or when the evidence is
   missing.

## Escalation Rules

- Identity drift, a location or time that changed without a declared transition, or an intentional-looking
  jump with no traceable approval: `ESCALATE`, `approval_required: true`, to the Director. If it was approved,
  it is traceable to a decision; if it is not traceable, it was not approved.
- A repair that would need a new shot, a changed line or a different action: `ESCALATE` to the Shot Planner
  through the Director, never a silent bridge.
- Missing end-frame evidence on a `CONTINUOUS` transition: `ESCALATE` with the evidence gap named.
- The runtime enforces the escalation. A Skill-driven `ESCALATE` (or `approval_required: true`) blocks the
  target shot's prompt compilation and generation until a real user acknowledges that decision or a later
  Skill-driven review replaces it. When the handoff's inputs change (a registered end frame, a redesigned
  plan), the next attempt to generate runs the review again - once per change, not once per attempt - so a
  verdict is re-reached on the current evidence rather than inherited; a person may still approve a verdict
  whose inputs have moved, and the approval records that it was stale. A deterministic comparison that stood
  in for the model is advisory: it records `approval_required` and never blocks, because it did not read the
  director's text and a fallback must not pass off as the Skill's verdict.

## Output Contract

One `ContinuityReview` object: `verdict` (`PASS` | `REPAIRABLE` | `ESCALATE`), `matched_state`,
`mismatches` (`field`, `previous`, `next`, `severity`), `evidence`, `minimal_repair` (or null),
`approval_required` and `unresolved`. No camera, lighting, action, dialogue, model or provider field; a
review carrying one is refused as a whole.

## Unresolved Policy

If a comparison cannot be made because the evidence is missing, the transition is unrecorded or an asset
version is unresolved, say so in `unresolved` and never fill the gap with a plausible state. A fabricated
bridge hides the defect and survives into every downstream shot.
