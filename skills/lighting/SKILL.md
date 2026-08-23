---
name: lighting
description: Design or repair motivated lighting for portraits, beauty, products, commercial scenes and connected video shots. Use when a shot needs explicit light direction, softness, contrast, material reflection control, foreground-background harmonisation, or continuity-safe relighting.
metadata:
  category: lighting
---

# Lighting

## Position in the pipeline

After framing, before compilation. This stage produces a physical lighting plan - where light comes from, how
large and how hard it is, and what it does to the specific materials in frame.

A plan, not a fixture list. Naming a softbox describes equipment; naming direction, size, distance and falloff
describes the image. Renderers respond to the second and ignore the first.

## Specify

1. **Motivation.** Window, sky, practical, sun, signage or studio source, justified by something in the scene.
   Unmotivated light is the most common reason a frame reads as artificial while every individual element looks
   correct.
2. **Key.** Direction, height, size, distance, hardness, intensity, falloff. Size and distance together set
   shadow edge quality - they are one decision, not two.
3. **Fill.** Direction and key-to-fill relationship. Use negative fill when contour needs recovering rather than
   adding another source.
4. **Colour and exposure.** Temperature relationships between sources, and which element the exposure protects.
5. **Shaping.** Shadow density, rim control, background separation, specular control.
6. **Material response.** Reflection gradients for metal, edge and transmission light for glass, broad diffusion
   for frosted surfaces, grazing light for texture. Material dictates the plan; a plan that ignores it produces a
   correct-looking light on a wrong-looking object.
7. **Composite and relight.** Evaluate foreground and background illumination separately, then harmonise
   direction, intensity, colour, contact shadow and spill. Mismatched contact shadow is what makes a composite
   read as a composite.

## Preserve

- Source direction, time of day, practical positions and exposure family across connected shots. Light direction
  is continuity, not styling - it changes where the scene is in time and space.
- Authentic skin microtexture. Control T-zone and lip highlights; smoothing them away produces plastic skin that
  no exposure adjustment recovers.
- Labels, logos, cap edges, glass transmission and product colour.
- Identity, pose, environment and story facts. Lighting supports the approved emotion; it does not restage the
  moment.

## Output

Return `motivation`, `key`, `fill`, `negative_fill`, `rim`, `background`, `temperature`, `exposure_priority`,
`material_controls` and `continuity_checks`.
