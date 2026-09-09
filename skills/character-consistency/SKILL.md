---
name: character-consistency
description: Method reference for preserving a canonical character identity across image regeneration, edits, storyboards and connected video shots while allowing only explicitly requested changes - identity anchors, reference views, drift checks, and carrying wardrobe, injury and prop state through a transition. Consulted by the Continuity stage and by the identity lock; not a separate runtime agent.
metadata:
  category: character-consistency
  role: character_consistency_reference
  stage: CONTINUITY
  runtime: reference
  bound_to: continuity
  authority:
    - identity_layer_separation
    - reference_view_selection
    - editable_variable_listing
    - temporal_state_carry
    - drift_evidence_rules
  forbidden_authority:
    - identity_version_creation
    - canonical_promotion
    - story_action
    - dialogue
    - framing
    - lighting
    - model
    - provider
  output_contract: identity invariants, editable variables and consistency checks folded into the Continuity review and the identity lock
  escalates_to: continuity
---

# Character Consistency

## Purpose

Decide what must not move, what is allowed to move, and what evidence is required before a change to a known
person is accepted as real. Collapsing those layers is how identity drifts without anyone noticing: a
wardrobe change smuggles in a jaw change, and because both arrived in one "edit", neither is attributable.

## Pipeline Position

Cross-cutting reference wherever a known person is rendered again. The identity lock
(`CharacterIdentityService`) binds versions by this method; the Continuity stage compares identity, wardrobe
and carried state by it. No runtime operation of its own.

## Inputs

The approved identity version and its content fingerprints and provider media bindings; the reference views
available; the previous final frame as temporal evidence; the exact variables the user asked to change.

## Authority

| Layer | Contents | How it may change |
| --- | --- | --- |
| Immutable identity | Facial structure and proportions, eye geometry and spacing, nose and lip geometry, jawline, skin tone, hairline, recognisable asymmetry, signature traits, canonical wardrobe design | Only by issuing a new identity version. An ordinary edit may never touch it. |
| Mutable narrative state | Injury and blood, wardrobe damage, contamination, wetness, held props, location, time, lighting, emotional beat | Only through an approved, evidence-backed change bound to a specific shot |
| Editable variables | Exactly what the user asked to change in this revision | Freely, this revision only |

Selecting reference views, listing editable variables, carrying temporal state, and the evidence rules for
accepting or rejecting a rendered face.

## Forbidden Authority

Creating an identity version or promoting a generated frame to canonical (the identity lock does that on the
user's approval); changing story action, dialogue, framing or light; the model or provider.

## Invariants

- Bind the approved identity version and reuse its bindings; never re-describe a face in prose and hope for
  convergence.
- Every unrequested variable is invariant for this revision - expression, pose, camera, lighting, wardrobe,
  background included. "While we are here" changes are indistinguishable from drift after the fact.
- A canonical trait that changes intentionally is a new identity version; the old one is the only baseline
  future drift can be measured against.
- A rendered frame is an output, not a source of truth; it is evidence about state, never authority over
  canonical identity.
- Evidence rules are asymmetric: a confident mismatch rejects; missing, low-confidence, advisory or untrusted
  evidence goes to human review. Absent evidence is never a pass.

## Decision Rules

1. Bind the approved identity version and its provider media bindings.
2. Supply the reference views the target actually needs - front, profile, three-quarter, full body, back and
   hairstyle. A profile target rendered from a front reference invents the half it cannot see.
3. List exactly the variables the user asked to change; treat every other variable as invariant.
4. Carry wardrobe, hair state, makeup, injury, dirt, wetness, held props, screen position, body orientation
   and gaze target when the transition requires it.
5. Under occlusion, weigh hair, silhouette, costume and tracking continuity; face similarity alone cannot
   establish identity when the face is not visible.

## Escalation Rules

A confident mismatch on an immutable-identity trait is a rejection; missing or advisory evidence goes to
human review; a requested change to an immutable trait goes to the identity lock as a new version, never as
an edit.

## Output Contract

`identity_version`, `canonical_references`, `identity_invariants`, `editable_variables`, `temporal_state`,
`reference_roles` and `consistency_checks`, folded into the Continuity review's mismatches and evidence and
into the identity lock's lineage.

## Unresolved Policy

An identity trait, a wardrobe canon or a state that the bound version does not establish stays unresolved;
it is never inferred from a rendered frame or described into existence.
