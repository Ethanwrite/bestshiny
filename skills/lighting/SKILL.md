---
name: lighting
description: Method reference for motivated lighting - direction, size, hardness, fill, rim, background separation, colour temperature, exposure priority and material response - for portraits, beauty, products, commercial scenes and connected video shots. Consulted by the Cinematography stage, of which lighting is a sub-domain; it is not a separate runtime agent.
metadata:
  category: lighting
  role: lighting_reference
  stage: CINEMATOGRAPHY
  runtime: reference
  bound_to: cinematography
  authority:
    - light_motivation
    - key_light
    - fill_light
    - rim_light
    - background_separation
    - color_temperature
    - exposure_priority
    - material_response
  forbidden_authority:
    - action
    - dialogue
    - identity
    - framing
    - camera_movement
    - continuity_verdict
    - model
    - provider
  output_contract: lighting fields of CinematographyPlan
  escalates_to: cinematography
---

# Lighting

## Purpose

A physical lighting plan - where light comes from, how large and how hard it is, and what it does to the
specific materials in frame. A plan, not a fixture list: naming a softbox describes equipment; naming
direction, size, distance and falloff describes the image. Renderers respond to the second and ignore the
first.

## Pipeline Position

A sub-domain of the Cinematography stage, applied after framing and before compilation. This Skill is
reference method for the `cinematography` Skill's `lighting` decisions; it has no runtime operation of its
own and is never injected alone.

## Inputs

The approved shot as Cinematography received it: action, states, gaze, cast and canonical bindings by
reference, the scene's time and location, the locked style, the previous shot's light where connected.

## Authority

Motivation, key, fill, negative fill, rim, background separation, temperature relationships, exposure
priority, shaping, material controls, and the composite harmonisation of foreground against background -
all exercised through the Cinematography stage's plan.

## Forbidden Authority

Identity, pose, environment and story facts; framing, lens and camera movement; the continuity verdict; the
model or provider. Light supports the approved emotion; it does not restage the moment.

## Invariants

- Source direction, time of day, practical positions and exposure family carry across connected shots. Light
  direction is continuity, not styling - it changes where the scene is in time and space.
- Authentic skin microtexture: control T-zone and lip highlights; smoothing them away produces plastic skin
  that no exposure adjustment recovers.
- Labels, logos, cap edges, glass transmission and product colour survive the plan.
- Unmotivated light is the most common reason a frame reads as artificial while every element looks correct.

## Decision Rules

1. **Motivation.** Window, sky, practical, sun, signage or studio source, justified by something in the
   scene.
2. **Key.** Direction, height, size, distance, hardness, intensity, falloff. Size and distance together set
   shadow edge quality - one decision, not two.
3. **Fill.** Direction and key-to-fill relationship; negative fill when contour needs recovering rather than
   another source.
4. **Colour and exposure.** Temperature relationships between sources, and which element the exposure
   protects.
5. **Shaping.** Shadow density, rim control, background separation, specular control.
6. **Material response.** Reflection gradients for metal, edge and transmission light for glass, broad
   diffusion for frosted surfaces, grazing light for texture. Material dictates the plan.
7. **Composite and relight.** Evaluate foreground and background illumination separately, then harmonise
   direction, intensity, colour, contact shadow and spill. Mismatched contact shadow is what makes a composite
   read as a composite.

## Escalation Rules

A light the scene cannot motivate, a material whose true response is unknown, or a direction that would
break the established continuity is reported to Cinematography as unresolved rather than approximated.

## Output Contract

The `lighting` block of the `CinematographyPlan`: `direction`, `quality`, `contrast`, `color_temperature`,
`practicals`, `motivation`, `exposure_intent`, plus material controls and continuity checks in the plan's
`continuity_checks`.

## Unresolved Policy

Decide free lighting variables from the approved intent and the material. Leave unresolved anything that
depends on an invariant not in hand - a product's canonical finish, a locked style's palette, an identity's
skin tone - and reference the asset instead of describing it.
