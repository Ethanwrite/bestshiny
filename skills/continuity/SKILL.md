---
name: continuity
description: Audit and repair state handoffs between connected image or video shots while preserving approved facts and asset versions. Use when comparing one shot's end state with the next shot's start state, checking screen direction or camera axis, carrying physical and prop state, binding end-frame evidence, or diagnosing a visible continuity break.
---

# Continuity

Treat the previous shot's approved end state and the next shot's start state as a contract. A discontinuity is
valid only when the story or user explicitly approves it.

## Check the handoff

1. Resolve the authoritative project, scene, shot order, character identity versions, environment versions, and
   promoted reference assets before comparing states.
2. Compare character identity, wardrobe, hair and makeup state, injury, dirt or wetness, held props and hands,
   body orientation, screen side, movement direction, and gaze target.
3. Compare environment topology, object positions, time, weather, motivated light direction, exposure family,
   camera axis, and screen direction.
4. Confirm the first action in the next shot starts from the actual completed end state; do not infer an
   unshown pickup, turn, prop transfer, or location change.
5. Use a registered `END_FRAME` image as visual evidence. A previous video ID or provider job ID is not an
   end-frame asset; extract, register, and bind the distinct frame before using it.
6. Classify each mismatch as `PASS`, `REPAIR`, or `CREATIVE_DECISION_REQUIRED`. Prefer the smallest repair that
   restores the contract without changing story action or canonical identity.

## Boundaries

- Do not invent a transition, prop transfer, new action, or asset version to hide a mismatch.
- Do not redesign framing, camera movement, lighting, or composition; identify the exact state they must retain.
- Do not silently promote a generated frame or overwrite a canonical asset.
- Keep intentional jumps explicit and traceable to an approved story decision.

Return `from_shot`, `to_shot`, `authoritative_assets`, `matched_state`, `mismatches`, `evidence`, `verdict`,
`minimal_repair`, and `approval_required`.
