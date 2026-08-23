---
name: continuity
description: Audit and repair the state handoff between connected image or video shots, preserving approved facts, identity versions and asset bindings. Use when comparing one shot's end state with the next shot's start state, checking screen direction or camera axis, carrying physical, wardrobe and prop state across a transition, binding end-frame evidence, or diagnosing a visible continuity break.
metadata:
  category: continuity
---

# Continuity

## Position in the pipeline

Between any two connected shots. The previous shot's approved end state and the next shot's start state form a
contract; this stage decides whether the contract holds, and if not, what the smallest honest repair is.

A discontinuity is not automatically an error. It is an error only when nobody approved it. The job is to tell
those two cases apart and never to quietly convert one into the other.

## The transition decides what may carry

Resolve the declared transition before comparing anything, because it determines which comparisons are even
meaningful:

| Transition | Effect on state |
| --- | --- |
| `CONTINUOUS` | Committed character, prop and costume state propagates. Every mismatch is a real mismatch. |
| `SCENE_CUT`, `LOCATION_CHANGE` | Spatial state resets. Identity and committed physical state still carry. |
| `TIME_JUMP`, `FLASH_FORWARD`, `MONTAGE` | State cannot simply carry; the handoff requires explicit reconciliation. |
| `FLASHBACK`, `DREAM` | A separate branch. Do not advance or contaminate the main timeline from inside it. |
| `EXPLICIT_RESET` | The contract is deliberately broken. Verify it was approved, then stop comparing. |

A transition label explains why the next frame follows this one. It never licenses inventing what happened in
between.

## Audit the handoff

1. **Resolve authority first.** Project, scene, shot order, character identity versions, environment versions and
   promoted reference assets. Comparing against the wrong version produces confident nonsense.
2. **Compare the subject.** Identity, wardrobe, hair and makeup state, injury, blood, dirt, wetness, held props
   and which hand holds them, body orientation, screen side, direction of travel, gaze target.
3. **Compare the world.** Environment topology, object positions, time, weather, motivated light direction,
   exposure family, camera axis, screen direction.
4. **Test reachability.** The next action must start from the end state that was actually rendered. An unshown
   pickup, turn, prop transfer or location change is a missing shot, not a detail to smooth over.
5. **Bind real evidence.** Use a registered `END_FRAME` image. A video asset ID or a provider job ID is not an
   end frame - extract, register and bind the distinct frame before reasoning about it. Evidence that was never
   extracted cannot support a verdict.
6. **Classify each mismatch** as `PASS`, `REPAIR` or `CREATIVE_DECISION_REQUIRED`, and propose the smallest
   repair that restores the contract without touching story action or canonical identity.

## Boundaries

- Never invent a transition, prop transfer, action or asset version to explain a mismatch away. A fabricated
  bridge hides the defect and survives into every downstream shot.
- Never redesign framing, movement, lighting or composition. Name the state they must preserve and return the
  decision to the stage that owns it.
- Never promote a generated frame to canonical or overwrite an approved asset.
- Escalate an intentional-looking jump rather than absorbing it. If it was approved, it is traceable to a
  decision; if it is not traceable, it was not approved.

## Output

Return `from_shot`, `to_shot`, `authoritative_assets`, `matched_state`, `mismatches`, `evidence`, `verdict`,
`minimal_repair` and `approval_required`.
