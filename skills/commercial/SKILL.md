---
name: commercial
description: Design brand-safe commercial and product imagery that preserves product geometry, packaging, labels, logos, colour, material, claims and offers. Use for studio packshots, campaign heroes, marketplace cards, e-commerce sets, beauty, food, lifestyle placements, or a commercial quality check.
metadata:
  category: commercial
---

# Commercial Production

## Position in the pipeline

Wherever a real product is rendered. The product and its brand facts are canonical invariants: not a starting
point, not a reference to interpret. A visually similar product is the wrong product, and an improved claim is a
false claim.

The stakes differ from narrative work. A drifted face is a quality problem; a drifted label is a
misrepresentation that ships.

## Choose the deliverable

| Mode | Use when |
| --- | --- |
| Packshot | Shape, label, colour, margin and marketplace readability must be exact |
| Detail | Material, applicator, texture, ingredient or a specific feature is the evidence |
| Lifestyle | Environment and human interaction explain use - without hiding the product |
| Campaign hero | One concept, strong hierarchy and copy-safe negative space carry the frame |
| Set | Several assets share a product plate, palette, lighting grammar, camera height and crop rules |

## Design

1. **Lock the invariants.** Silhouette, proportions, label, logo, typography, closure, colour, material,
   required text.
2. **Choose angle and lens behaviour** that reveal the intended feature without distorting geometry. A flattering
   perspective that changes proportions has changed the product.
3. **Light for the material** and control the reflections it produces.
4. **Define separation** - product against background, contact shadow, scale cues, hierarchy, copy-safe space.
5. **Keep compliance and styling apart.** A clean main listing image is not the place for decorative props or
   campaign claims; the two have different acceptance rules and mixing them fails both.
6. **Vary one control at a time** and compare every result against the canonical plate. Multi-variable
   iterations produce a result nobody can attribute or reproduce.

## Quality gate

- Reject altered packaging, invented text, broken logos, wrong colourways, duplicated components, floating
  contact shadows and physically inconsistent reflections.
- Required product text stays verbatim and legible. When generation cannot hold text reliably, ask for
  deterministic post-compositing rather than accepting an approximation - near-correct text is worse than none,
  because it is read as real.
- The focal product stays readable without breaking scene continuity.
- Reconcile a paid attempt against its existing job; a new creative direction is a new attempt, not a retry.

## Output

Return `product_invariants`, `deliverable_mode`, `composition`, `material_lighting`, `background`,
`copy_safe_area`, `required_text` and `quality_checks`.
