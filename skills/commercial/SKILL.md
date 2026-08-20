---
name: commercial
description: Design brand-safe commercial and product imagery while preserving product geometry, packaging, labels, logos, color, material, claims, and offers. Use for studio product shots, campaign heroes, marketplace cards, e-commerce image sets, beauty products, food, lifestyle placements, or commercial quality checks.
---

# Commercial Production

Treat the approved product and brand facts as canonical invariants. Never replace them with a visually similar
product or improve a claim.

## Choose the deliverable

- Use a clean packshot for accurate shape, label, color, margin, and marketplace readability.
- Use a detail shot for material, applicator, texture, ingredient, or feature evidence.
- Use a lifestyle shot when environment and human interaction explain use without hiding the product.
- Use a campaign hero when one strong concept, visual hierarchy, and copy-safe negative space matter.
- Build a set from a shared product plate, palette, lighting grammar, camera height, and crop rules.

## Design

1. Lock product silhouette, proportions, label, logo, typography, closure, color, material, and required text.
2. Choose camera angle and lens behavior that reveal the intended feature without distorting geometry.
3. Choose material-specific lighting and controlled reflections.
4. Define product/background separation, contact shadow, scale cues, visual hierarchy, and copy-safe space.
5. Separate listing compliance from campaign styling; do not mix claims or decorative props into a clean main
   listing image.
6. Generate or edit variants one controlled change at a time and compare every result with the canonical plate.

## Quality gate

- Reject altered packaging, invented text, broken logos, wrong colorways, duplicated components, floating
  contact shadows, or physically inconsistent reflections.
- Keep required product text verbatim and readable; request deterministic post-compositing when generation is
  not reliable enough.
- Keep the focal product readable without breaking scene continuity.
- Reuse the same submitted generation job when reconciling a paid call; create a new explicit attempt for a new
  creative generation.

Return `product_invariants`, `deliverable_mode`, `composition`, `material_lighting`, `background`,
`copy_safe_area`, `required_text`, and `quality_checks`.
