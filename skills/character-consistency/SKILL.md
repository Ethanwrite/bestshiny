---
name: character-consistency
description: Preserve a canonical character identity across image regeneration, edits, storyboards and connected video shots while allowing only explicitly requested changes. Use when selecting identity anchors, binding reference views, regenerating a known person, checking drift, or carrying wardrobe, injury and prop state through a transition.
metadata:
  category: character-consistency
---

# Character Consistency

## Position in the pipeline

Wherever a known person is rendered again. This stage decides what must not move, what is allowed to move, and
what evidence is required before a change is accepted as real.

## Three layers, never merged

| Layer | Contents | How it may change |
| --- | --- | --- |
| Immutable identity | Facial structure and proportions, eye geometry and spacing, nose and lip geometry, jawline, skin tone, hairline, recognisable asymmetry, signature traits, canonical wardrobe design | Only by issuing a new identity version. An ordinary edit may never touch it. |
| Mutable narrative state | Injury and blood, wardrobe damage, contamination, wetness, held props, location, time, lighting, emotional beat | Only through an approved, evidence-backed change bound to a specific shot |
| Editable variables | Exactly what the user asked to change in this revision | Freely, this revision only |

Collapsing these layers is how identity drifts without anyone noticing: a wardrobe change smuggles in a jaw
change, and because both arrived in one "edit", neither is attributable.

## Lock identity

1. Bind the approved identity version and reuse its content fingerprints and provider media bindings. Reuse a
   binding; never re-describe a face in prose and hope for convergence.
2. Supply the reference views the target actually needs - front, profile, three-quarter, full body, back and
   hairstyle. A profile target rendered from a front reference invents the half it cannot see.
3. Preserve body silhouette, age cues and distinguishing details across every view.

## Apply an edit

1. List exactly the variables the user asked to change.
2. Treat every unrequested variable as invariant for this revision - expression, pose, camera, lighting,
   wardrobe, background included. "While we are here" changes are indistinguishable from drift after the fact.
3. When a canonical trait changes intentionally, create a new identity version. Never overwrite the previous
   one: the old version is the only baseline future drift can be measured against.

## Carry state and judge evidence

- Carry wardrobe, hair state, makeup, injury, dirt, wetness, held props, screen position, body orientation and
  gaze target when the transition requires it.
- Use the previous final frame as temporal evidence about state. It is never authority over canonical identity -
  a rendered frame is an output, not a source of truth.
- Under occlusion, weigh hair, silhouette, costume and tracking continuity. Face similarity alone cannot
  establish identity when the face is not visible, and a confident score on a hidden face is a false positive.
- Evidence rules are asymmetric and stay that way: a confident mismatch rejects. Missing, low-confidence,
  advisory or untrusted evidence goes to human review. Absent evidence is never a pass.

## Output

Return `identity_version`, `canonical_references`, `identity_invariants`, `editable_variables`, `temporal_state`,
`reference_roles` and `consistency_checks`.
