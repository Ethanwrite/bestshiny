# Higgsfield ONEIRIC — public production trace

Research snapshot: 2026-08-30

Source: [ONEIRIC full film and public project](https://higgsfield.ai/original-series/oneiric/full-film)

## Verdict

ONEIRIC is a useful **external production case study**, not a production posterior. The public
project exposes enough data to prove most of this chain:

    named reference asset ──exact──▶ prompt ──exact──▶ generation job ──exact when public──▶ output asset
                                          │
                                          └──exact──▶ scene / shot folder membership

The last edge is not public:

    output asset ──UNOBSERVED──▶ selected shot in the final edit

The final film is published as one 1,189-second asset, but the page does not expose an edit
decision list, timeline, selected-take flag, or source-generation ID per final-film time range.
Consequently, no honest importer can currently answer “this exact frame in the final film came
from generation X.” It can list candidates in the relevant shot folder and their exact chronology.

## What the source actually exposes

The source intentionally publishes prompts (`project_publication.show_prompts = true`) and a
snapshot folder. The public JSON responses observed on 2026-08-30 include:

- project/episode/publication IDs and the final film asset;
- a project-wide stated count of **41,096 generations**;
- a root snapshot containing **41,118 items** in **16** top-level folders;
- an `ASSETS` bucket containing **22,960 items**;
- a `regenerations` bucket containing **292 items**;
- 14 scene folders, with nested `shot 1`, `shot 2`, and similar folders where used;
- for a generation job: `id`, `status`, `created_at`, `job_set_id`, `job_set_type`, prompt,
  duration, resolution, aspect ratio, audio flag, named reference elements and their media;
- an output `result` / `results` URL where that output remains public.

The item count is deliberately not called a generation count: `41,118 != 41,096`, and folders can
hold things other than one billable model attempt. Likewise a `job_set_id` groups batch siblings;
it is not a retry edge. `job_set_parent_id` is retained when present but is not populated for the
sampled ONEIRIC jobs.

Two concrete public traces demonstrate the available joins:

- generation `35aca6ad-a259-4d8d-8279-474e2641d0ad` exposes an image prompt, model family
  `soul_cinematic`, seed/style parameters and an output image URL containing the generation ID;
- generation `553e19f5-f9ac-44c8-8d07-2dc9df9920ac` in `regenerations` exposes a full Seedance
  prompt plus named character/location reference elements. Its status is `completed`, not failed,
  and its public result is null.

That second example is the reason the adapter uses `CREATIVE_REWORK_CANDIDATE`: placement in a
folder named `regenerations` is evidence that the team revisited material, not evidence of a
provider outage, invalid request, or technically failed generation.

## What “前面失败了什么” can and cannot mean

The importer keeps three categories separate:

1. `PROVIDER_FAILURE` — only an explicit failed/error/cancelled/rejected job status.
2. `CREATIVE_REWORK_CANDIDATE` — a job filed under `regenerations`, even when its status is
   completed. This may reflect acting, staging, continuity, dialogue, or taste; the public data
   does not state which.
3. `COMPLETED_CANDIDATE` — a completed generation in any other folder. Completion does not imply
   acceptance or use in the edit.

Earlier jobs in the same shot folder are recorded as `previous_candidate_ids`, ordered by the
source timestamp. They are not called failures unless the source status says so. Prompt hashes make
revisions diffable without pretending that every textual change fixed a known defect.

## Integration

The committed registry is
[`config/production-evidence/sources-v1.json`](../config/production-evidence/sources-v1.json). Its
ONEIRIC entry is shared by both operator surfaces. The wheel build copies this manifest into the
`router_evidence_core` package, so an installed API reads the same registry without depending on a
source-checkout-relative path:

- `GET /internal/models/router-evidence` returns it under
  `external_production_case_studies` with `posterior_eligible: false` and explicit exclusion
  reasons;
- `GET /internal/production-evidence/sources` returns the lineage capabilities and limitations.

The live reader and schemas are in
[`public_project_sources.py`](../core/router-evidence/router_evidence_core/public_project_sources.py).
To materialise a bounded snapshot:

```bash
.venv/bin/python scripts/ingest_public_project_evidence.py \
  --folder 'SCENE 2 - LIVINGROOM/shot 1' \
  --folder regenerations \
  --max-items-per-folder 100
```

The output goes to ignored local data at
`data/production-evidence/public-projects/higgsfield-oneiric-2026-08.json`. It contains the typed
snapshot plus `router_evidence` and `production_evidence` projections. Whole-project crawling is
never implicit; callers must select exact folders and a per-folder cap. Descendant folders are not
folded into their parent, because doing so would invent cross-shot lineage. An existing output is
preserved by default; pass `--force` only when replacing that snapshot is intentional. Publication
uses a complete temporary file and an atomic replace, so an interrupted writer cannot leave partial
JSON at the destination.

## Router boundary

This source is excluded from the posterior for five independent reasons:

- it is an external vendor-produced project, not traffic from this platform;
- `job_set_type` does not identify an exact provider weight revision;
- no final-selection edge is published;
- no calibration bridge maps these project traces to a `prod.*` outcome scale;
- the published project is curated and therefore selection-biased.

The data is still valuable for prompt-structure research, reference-mode coverage, generation
workflow design, retry taxonomy and production UI lineage. It simply cannot be converted into
accepted-output or provider-failure rates.
