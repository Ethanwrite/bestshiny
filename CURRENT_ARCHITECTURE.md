# Current Architecture and Incremental Target

Audit date: 2026-08-19. Baseline: 39 tests pass; Ruff and Mypy pass. This document records the
repository as it exists before the Passenger Seat / Autopilot runtime upgrade.

## Current repository

The platform is a Python 3.12 modular monolith with FastAPI, SQLAlchemy/Alembic, a background
generation worker, a Chrome browser worker for Google Flow, PostgreSQL/pgvector in Docker,
SQLite for tests, local/S3-compatible media storage, and a static responsive Web application.

Important boundaries:

- `packages/domain`: production, provider, media, candidate, QA, cost, decision and skill records.
- `packages/contracts`: typed API request contracts.
- `packages/provider-sdk`: provider execution interface; provider calls do not live in agents.
- `services/generation-gateway`: durable idempotent generation, account scheduling, retries and events.
- `services/media-service`: SHA-256 media registration, storage and provider-media reuse.
- `services/production-engine`: project/shot operations and end-frame continuity.
- `core/*`: narrative, identity, continuity decision, policy, QA, cost, prompt and commit pipeline.
- `providers/*`: Google Flow implementation and honest unconfigured provider slots.
- `apps/api`, `apps/web`, `apps/browser-worker-extension`: product entry points.

## Current production flows

### Direct/manual path

`POST /v1/generations` and the OpenAI-compatible image/video endpoints accept a user-selected
provider/model and submit through the shared `GenerationGateway`. Asset upload also uses the
shared `MediaRegistry`. This is a reusable Passenger Seat foundation, but the Web application
does not yet expose a complete manual image/video workspace or asset promotion flow.

### Directed shot path

`NarrativeCompiler → CandidatePipeline → CapabilityResolver → PromptCompilerService →
GenerationGateway → QAPipeline → CandidatePipeline.commit`.

This already provides a useful Autopilot foundation: persistent candidates, capability fallback,
provider-neutral job execution, QA gates, end-frame extraction, timeline-state persistence and cost
records. Passenger Seat and Autopilot already share the gateway and media registry; this invariant
must be preserved.

## Current model APIs and routing

- `GenerationProvider` defines image/video generation, upload, asset validation, polling,
  cancellation, credits and health.
- `ProviderRouter` maps provider names to execution implementations.
- `ProviderCapabilityRegistry` currently stores provider-level booleans in Python code.
- `CapabilityResolver` selects a capability-compatible provider and uses observed account success,
  latency and cost, but does not rank individual versioned models or apply task-specific capability
  and failure priors.
- A current fallback can change the provider while retaining the original model string, for example
  selecting Seedance while the request still names `veo`; Phase 2 must make provider/model selection atomic.
- Google Flow has a provider-specific mapper. There is no shared `CanonicalShotSpec → ModelAdapter`
  contract for Kling/Veo/Seedance/Grok/Wan.

Reusable: provider SDK, gateway, scheduler, event log, account bindings, cost records and the current
capability-degradation behavior.

Required change: introduce a configuration-backed model registry and a deterministic explainable
model router beside the existing resolver. Keep the old resolver API as a compatibility facade.

## Current asset logic

`MediaAsset` is a content-addressed artifact with project, character, scene, shot, parent and
generation-candidate links. `CharacterIdentityVersion` is immutable after confirmation. `Location`
and `Prop` have canonical asset links. Generated results cannot currently replace a locked identity
automatically.

Reusable: media deduplication, storage, asset lineage, provider media bindings, identity-version
locking, locations, props and canonical character references.

Required change: add a unified logical `Asset` plus immutable `AssetVersion` above `MediaAsset`,
support character/scene/product/prop/wardrobe/vehicle/creature/voice/style/reference kinds, and
require explicit promotion before a version becomes canonical. Existing media and identity tables
remain valid and are linked rather than replaced.

## Current prompt logic

`PromptCompilerService` currently contains both a visible whitespace/punctuation `refine` operation
and the internal shot compiler. The shot compiler directly embeds provider-specific text branches.
This violates the new separation boundary.

Reusable: `PromptCompilation`, original/compiled prompt audit, skill-version recording, identity and
timeline-state lookup.

Required change:

1. Create a user-visible `ImagePromptCorrector` with typed task detection, invariant preservation,
   undo data and its own Skill knowledge library.
2. Create `CanonicalShotSpec` and a separate internal `VideoShotPromptCompiler`.
3. Move model wording and payload details into model adapters; no common string is sent unchanged to
   every model.

## Current memory and context

`TimelineState` and `ShotStateSnapshot` represent temporal state. Four 16-dimensional deterministic
hash vectors are stored, but there is no semantic/multimodal embedding provider, metadata-filtered
retrieval, L0/L1/L2 memory model, Voyage integration or bounded context assembly.

Reusable: timeline state, snapshots, narrative events, committed candidate assets and pgvector.

Required change: add `ShotMemory`, configurable 512-dimensional embeddings, an embedding-provider
interface with optional Voyage Multimodal 3.5 implementation, metadata-first retrieval, canonical
and recency reranking, and a `ContextAssembler` with explicit text/image/video budgets. The feature
is disabled safely when Voyage credentials are absent.

## Current evaluation, retry and metrics

`QAPipeline` performs file validation, evidence-based seven-dimension scoring, identity drift and
hard gates. Gateway retry protects uncertain paid submissions. These are separate concerns and
should remain separate.

Reusable: QA records, hard failures, candidate statuses, retry policy, cost records, generation
events and decision records.

Required change: add a production `GenerationEvaluator` decision contract (`ACCEPT`, retry same
model, rewrite prompt, switch model, reject), critical failures for gaze/costume/prop/screen direction,
a bounded `RetryEngine`, structured retry patches and model production metrics. Never use Voyage
similarity as the sole identity verdict.

## Current Web and API

The Web UI is a three-column director workspace for script compilation, shot candidates, QA,
commit, identity version upload, continuity and basic prompt cleanup. It primarily represents
Autopilot. Direct generation and asset upload APIs already exist, but there is no clear Passenger
Seat / Autopilot mode switch.

Required change: add a simple Passenger Seat page/section for image/video mode, prompt correction,
manual model selection, transparent duration/resolution/cost and promote-to-asset. Internal compiled
video prompts, router scores and cinematography instructions stay hidden by default.

The current provider selector and camera/lighting controls are presentational: the shot-generation
handler does not send their values. They must either become real inputs or be hidden, never shown as
working controls when they are not wired.

## Data gaps

Additive tables required:

- `assets`, `asset_versions`
- `shot_memories`
- `model_capabilities`, `model_metrics`, `model_benchmarks`
- `prompt_revisions`
- `evaluation_results`
- `feature_flags`

Existing `projects`, `media_assets`, `character_identity_versions`, `timeline_states`, `shots`,
`generation_jobs`, `generation_candidates`, `qa_results`, `cost_records`, `decision_records` and
`generation_events` are retained.

## Target runtime without duplicate engines

```text
Passenger Seat ─┐
                ├─ Asset Registry → Memory/Context → Model Router → Generation Gateway
Autopilot ──────┘                                      │
                                                      ▼
                                    Evaluation → Accept/Retry/Switch
                                                      │
                                                      ▼
                                             Project Memory
```

Passenger Seat may bypass automatic routing when the user explicitly selects a model. It does not
bypass the shared gateway, cost recording, artifact storage or optional evaluation.

## Concrete incremental file plan

1. Phase 2: replace the hard-coded capability collection behind its public facade with
   `core/model-routing/`, `config/video-models/*.json`, typed schemas and tests.
2. Phase 3: add `core/image-prompt/`, `/api/prompt/correct`, prompt revisions, the
   `skills/image-prompt-corrector/` library and Passenger Seat controls.
3. Phase 4: add `core/assets/`, additive domain tables/migration and promote/list/search APIs.
4. Phase 5: add `core/memory/`, Voyage-compatible embedding client, retrieval/context schemas,
   memory APIs and feature flags.
5. Phase 6: add `core/video-prompt/`, `adapters/video/`, canonical shot schema and integrate the
   candidate pipeline without removing old API paths.
6. Phase 7: add `core/evaluation/`, evaluation API and a bounded retry coordinator around existing
   gateway retry safety.
7. Phase 8: add `core/benchmarks/`, model metrics, adaptive adjustments and observability APIs.

No phase will create a second generation gateway, media store or production engine.
