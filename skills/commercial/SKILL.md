---
name: commercial
description: Method reference for brand-safe commercial and product imagery that preserves product geometry, packaging, labels, logos, colour, material, claims and offers, across packshot, detail, lifestyle, campaign-hero and set deliverables. Consulted by the Director when locking product invariants and by Cinematography and the Prompt Compiler when rendering a real product; not a separate runtime agent.
metadata:
  category: commercial
  role: commercial_reference
  stage: STORY
  runtime: reference
  bound_to: director
  authority:
    - deliverable_mode
    - product_invariant_checklist
    - material_lighting_guidance
    - copy_safe_area
    - commercial_quality_gate
  forbidden_authority:
    - product_claim_text
    - product_geometry_change
    - story_action
    - identity
    - model
    - provider
  output_contract: product invariants and quality checks folded into the story invariants and the QC checklist
  escalates_to: director
---

# Commercial Production

## Purpose

Render a real product without misrepresenting it. The product and its brand facts are canonical invariants:
not a starting point, not a reference to interpret. A visually similar product is the wrong product, and an
improved claim is a false claim. The stakes differ from narrative work: a drifted face is a quality problem;
a drifted label is a misrepresentation that ships.

## Pipeline Position

Cross-cutting reference. The Director locks product facts and claims as story invariants using this method;
Cinematography lights and frames the product by it; the Prompt Compiler's QC checklist carries its quality
gate. No runtime operation of its own.

## Inputs

The canonical product plate and asset bindings, the brief's selling points and claims verbatim, required
on-screen text, the deliverable the client asked for, and the platform's compliance rules for that
deliverable.

## Authority

Choosing the deliverable mode; the checklist of product invariants to lock; material-driven lighting and
angle guidance; separation, hierarchy and copy-safe space; the quality gate every commercial frame must pass.

| Mode | Use when |
| --- | --- |
| Packshot | Shape, label, colour, margin and marketplace readability must be exact |
| Detail | Material, applicator, texture, ingredient or a specific feature is the evidence |
| Lifestyle | Environment and human interaction explain use - without hiding the product |
| Campaign hero | One concept, strong hierarchy and copy-safe negative space carry the frame |
| Set | Several assets share a product plate, palette, lighting grammar, camera height and crop rules |

## Forbidden Authority

The wording of any claim or offer; any change to product geometry, packaging, label, logo, colourway or
required text; story action; identity; the model or provider.

## Invariants

- Silhouette, proportions, label, logo, typography, closure, colour, material and required text are locked
  before anything is styled.
- Required product text stays verbatim and legible. When generation cannot hold text reliably, ask for
  deterministic post-compositing rather than accepting an approximation - near-correct text is worse than
  none, because it is read as real.
- Compliance and styling stay apart: a clean main listing image is not the place for decorative props or
  campaign claims.
- Vary one control at a time and compare every result against the canonical plate; multi-variable iterations
  produce a result nobody can attribute or reproduce.

## Decision Rules

1. **Choose the deliverable** before styling it.
2. **Lock the invariants** from the canonical plate and the brief, verbatim.
3. **Choose angle and lens behaviour** that reveal the intended feature without distorting geometry; a
   flattering perspective that changes proportions has changed the product.
4. **Light for the material** and control the reflections it produces.
5. **Define separation** - product against background, contact shadow, scale cues, hierarchy, copy-safe
   space.
6. **Gate the result**: reject altered packaging, invented text, broken logos, wrong colourways, duplicated
   components, floating contact shadows and physically inconsistent reflections. The focal product stays
   readable without breaking scene continuity.
7. **Reconcile a paid attempt against its existing job**; a new creative direction is a new attempt, not a
   retry.

## Escalation Rules

A claim the brief does not establish, a product fact the plate does not show, or a required text the
generation cannot hold is escalated to the Director (and through it to the client); it is never approximated.

## Output Contract

`product_invariants`, `deliverable_mode`, `composition`, `material_lighting`, `background`,
`copy_safe_area`, `required_text` and `quality_checks`, folded into the story's invariants and product claims
and into the compiler's QC checklist.

## Unresolved Policy

A product fact, claim, offer or legal wording that is unknown stays unresolved and escalates. Only styling
variables - background, prop dressing within compliance, light quality - may be decided freely.
