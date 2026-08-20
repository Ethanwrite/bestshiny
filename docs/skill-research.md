# Skill Research Record

Reviewed on 2026-08-19. This record documents architectural and visual-production methods studied for the
project Skills. No upstream prompt, code block, or Skill body was copied into this repository; the local material
is a concise, project-specific re-expression of general methods.

## Sources and licenses

| Source | Material reviewed | Repository license | Use in this project |
| --- | --- | --- | --- |
| [fal-ai-community/skills](https://github.com/fal-ai-community/skills) | `cinematography`, `storytelling`, `commercial`, `character-design`, and family-specific prompting structure | MIT | Adopted progressive disclosure, a stable subject/context/lens/camera/atmosphere/mood build order, invariant-versus-variable separation, concrete visual language, structured shot cards, and pre-generation quality gates. No fal endpoint commands or copied prompt examples were added. |
| [seaartpublic/skills](https://github.com/seaartpublic/skills) | `storyboard-prompt-assistant` and staged long-story shot planning | MIT | Adopted explicit shot start/end states, one dominant action, continuity handoff, gaze targets, and staged decomposition for longer stories. No upstream Skill body or prompt examples were copied. |
| [Hunyuan-PromptEnhancer/PromptEnhancer](https://github.com/Hunyuan-PromptEnhancer/PromptEnhancer) | Text-to-image rewriting, image-to-image edit-instruction refinement, intent preservation, structured enrichment, and fallback parsing | Tencent Hunyuan Community License Agreement | Adopted the process boundary `detect intent -> preserve subjects and constraints -> add observable visual detail -> remove conflicts -> rewrite`, plus explicit original/corrected prompt retention. No model, weight, training output, inference code, or licensed text was incorporated. The upstream license contains territory and use restrictions, so any future runtime integration requires separate legal review. |
| [replicate/skills](https://github.com/replicate/skills) | `prompt-images` and `prompt-videos` | Apache-2.0 | Adopted natural-language specificity, explicit edit scope, named subjects instead of ambiguous pronouns, model-schema checks, single-change iteration, chronological video action, and separation of generation instructions from provider controls. No Replicate API integration or copied Skill body was added. |
| [lllyasviel/IC-Light](https://github.com/lllyasviel/IC-Light) | Text-conditioned and background-conditioned relighting concepts | Apache-2.0 for the repository; upstream README notes separate restrictions for the bundled BRIA background-removal component and model artifacts may have their own terms | Adopted lighting-analysis concepts only: evaluate foreground and background illumination separately, then harmonize direction, intensity, color, contact shadow, spill, and material response. No code, checkpoint, dataset, or background-removal component was integrated. |
| [higgsfield-ai/skills](https://github.com/higgsfield-ai/skills) | Product photoshoot modes and marketplace-card asset scopes | MIT | Adopted deliverable-first commercial planning: distinguish packshot, detail, lifestyle, campaign hero, marketplace main image, secondary image, and content module; preserve a canonical product plate across variants. No CLI commands, private enhancement templates, or upstream Skill text was copied. |

## Absorbed methodology

### Cinematography

- Build a shot in a stable order: subject, context, lens/framing, camera, atmosphere, mood/color, and output
  constraints.
- Replace generic prestige words with screen position, gaze target, perspective, distance, light direction,
  blocking, focus behavior, and material response.
- Treat character and product references as invariants and cinematography as a controlled variable layer.
- End every design with physical-plausibility, axis, eyeline, continuity, and unsupported-control checks.

Applied to `skills/cinematography/`, `skills/camera-movement/`, and `skills/lighting/`.

### Storyboard execution and continuity

- Convert only director-approved actions into executable shot cards.
- Give every shot one visible dominant action, an explicit start state, an explicit end state, and a named gaze
  target.
- Treat the previous shot's end state and the next shot's start state as a continuity contract rather than a
  stylistic suggestion.
- Escalate unresolved story changes to the director instead of hiding them inside shot or camera instructions.

Applied to `skills/short-drama/`, `skills/composition/`, and `skills/continuity/`.

### Intent-preserving prompt correction

- Enhance observable visual decisions without redesigning the user's subject.
- Separate identity invariants from variables explicitly requested for editing.
- Preserve the original prompt and report changes so correction remains reversible.
- Remove conflicts before enrichment and avoid empty tokens such as camera brands, resolution slogans, or
  universal focal-length defaults.

Applied to `skills/image-prompt-corrector/`. The image corrector remains separate from the internal video-shot
compiler.

### Character identity

- Bind a versioned canonical identity rather than treating one generated image as the entire asset.
- Select reference views appropriate to the requested target view, including profile, three-quarter, full-body,
  and rear hairstyle evidence.
- Change only named variables; create a new version for intentional canonical changes.
- Use face, body, hair, wardrobe, and temporal tracking as complementary evidence rather than treating one
  embedding score as final truth.

Applied to `skills/character-consistency/`.

### Commercial imagery

- Choose the commercial deliverable before styling it.
- Lock product silhouette, proportions, packaging, label, logo, typography, color, material, and claims.
- Choose lighting from material behavior rather than adding `premium` or `luxury` adjectives.
- Keep marketplace compliance, campaign styling, lifestyle context, and detail evidence as distinct modes.
- Build variant sets from a shared product plate, palette, lighting grammar, crop rules, and explicit QA.

Applied to `skills/commercial/`.

### Model-specific prompting

- Keep visual intent in Skills and provider field mapping in adapters.
- Read the current model capability profile before using a control; do not treat remembered capabilities as
  permanent facts.
- Preserve a canonical shot specification, then express it concisely for the selected model family.
- Activate failure patches only when a shot requirement triggers a configured failure prior, such as final-frame
  direct gaze.
- Record unsupported requirements rather than hiding silent degradation in the prompt.

Applied to `skills/model-prompting/`. Its model notes are initial production hypotheses subordinate to the live
capability registry, benchmark results, and production metrics.

## Deliberate exclusions

- No upstream model or CLI was installed.
- No source repository was vendored into runtime code.
- No upstream code, trained weight, prompt template, private backend enhancer, or generated dataset was used.
- No model capability claim in these Skills overrides the project's configuration-driven capability registry.
- License identification is an engineering provenance record, not legal advice; runtime or model integration must
  re-check the exact source revision and artifact license.
