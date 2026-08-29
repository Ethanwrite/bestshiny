# Character Evidence → Modal handoff

Date: 2026-08-28

## Current branch and database

- Repository: `/Users/a1-6/Desktop/BestShiny/ai-director-platform`
- Git branch: `main`
- Base commit: `9a06dcf5ea921a43d006110d5073e22c851c1881`
- Local database: PostgreSQL at `127.0.0.1:5432`, database `video_platform` (credentials omitted)
- Alembic/database revision: `0052_shot_dependencies` — single head
- This work adds no migration. The current database number remains **`0052`**.
- State: uncommitted working-tree changes; no Character Evidence PR exists.
- The untracked `.worktrees/` directory predates this task and was not modified.

## Outcome

The code path is implemented and repository checks pass. The shared application keys and Modal
Secret now exist. The live Modal deployment and production smoke test are **not complete**; the next
window must first confirm that `api.bestshiny.com` is publicly HTTPS-reachable, then deploy.

The intended boundary is now represented in code:

```text
BestShiny
→ CharacterEvidenceProducer protocol
→ ModalCharacterEvidenceProducer
→ one authenticated Modal HTTPS endpoint
→ one Modal CV App / one T4 worker
→ YOLOX-s + ByteTrack + YuNet + aligned SFace + DINOv2-base
→ signed callback to BestShiny
→ shadow-only persistence
```

There is no OpenRouter/Alibaba/Volcano path, no Modal SDK in the domain layer, and no local
production inference fallback.

## Implemented

1. Production DI constructs `ModalCharacterEvidenceProducer` and injects it into `QAPipeline`.
   Production startup fails closed for a missing/non-HTTPS endpoint, weak keys, or a non-shadow mode.
2. Modal App `bestshiny-character-evidence` exposes only
   `POST /v1/character-evidence/analyze`, requires Bearer auth, spawns a worker, and returns 202.
3. The worker uses `gpu="T4"`, `min_containers=0`, `max_containers=1`, and
   `scaledown_window=60`; model instances load once per container and are reused.
4. The image build pins official source revisions and artifact SHA-256 values, downloads weights at
   build time, and verifies them before runtime.
5. The real pipeline performs FFmpeg-backed frame sampling, YOLOX person detection, ByteTrack using
   YOLOX observations, YuNet face detection, YuNet-landmark/SFace five-point alignment and embedding,
   and DINOv2 body appearance encoding.
6. Requests use short-lived HTTPS media URLs and immutable reference asset versions. Arbitrary local
   upload and local inference fallback are rejected.
7. Callback bodies use timestamped HMAC signatures. Callback run IDs are persisted idempotently.
   A 202 response is only acceptance and never evidence.
8. Every sample carries detector/tracker/face/appearance model versions, threshold version,
   reference asset ID/version, and pipeline version.
9. Decisions are `PASS`, `FAIL`, or `ABSTAIN`; uncertainty and conflicts route to review.
   `ABSTAIN` cannot be converted to PASS by the existing weighted QA calculation.
10. Shadow callbacks record observations without changing the existing candidate gate.
11. Hair and costume remain exactly `UNAVAILABLE`; DINOv2 is only general appearance evidence.
12. A versioned authorized-validation schema, per-slice metric calculator, empty collection-pending
    index, and prewritten promotion criteria were added. No mock data is presented as real validation.

## Pinned production artifacts

| Role | Model/revision | Artifact SHA-256 |
| --- | --- | --- |
| Person detection | YOLOX-s `0.1.1rc0` / `e1052df71842031413f6030723c3607b839c80ce` | `f55ded7181e1b0c13285c56e7790b8f0e8f8db590fe4edb37f0b7f345c913a30` |
| Tracking | ByteTrack / `d1bf0191adff59bc8fcfeaa0b33d3d1642552a99` | `7cecdcdd7998103969a4ba1772f4c9fb5560fd5eef05ca03e0d2df28346ca50b` |
| Face detection | YuNet `2026may` / `47534e27c9851bb1128ccc0102f1145e27f23f98` | `ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0` |
| Face identity | SFace `2021dec` / `47534e27c9851bb1128ccc0102f1145e27f23f98` | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` |
| Appearance | DINOv2-base `dinov2_vitb14` / `7764ea0f912e53c92e82eb78a2a1631e92725fc8` | `0b8b82f85de91b424aded121c7e1dcc2b7bc6d0adeea651bf73a13307fad8c73` |

Manifest: `services/character-evidence/character_evidence_model_manifest.json`  
Threshold version: `character-evidence-thresholds-2026-08-27-v1`  
Current operating mode: `SHADOW`

## Deployment configuration and current blocker

Modal CLI authentication is healthy:

- Workspace/profile: `uu6first`
- Environment: `main`
- Modal client: `1.5.4`

The first requested deployment command was actually executed before the application Secret existed:

```bash
modal deploy --env main --name bestshiny-character-evidence \
  services/character-evidence/modal_app.py
```

It exited 1 before build/deployment with:

```text
Secret 'bestshiny-character-evidence-secrets' not found in environment 'main'.
```

That original Secret blocker is now resolved. On 2026-08-28:

- `bestshiny-character-evidence-secrets` was created in `uu6first/main`.
- It contains the API key, callback-signing key, and
  `CHARACTER_EVIDENCE_CALLBACK_URL=https://api.bestshiny.com/v1/webhooks/character-evidence`.
- Two independent 256-bit random keys were generated in one process and written to both Modal and
  the Git-ignored local `.env`; their values were never printed or committed.
- Local `PUBLIC_BASE_URL` is now `https://api.bestshiny.com`.
- `CHARACTER_EVIDENCE_BASE_URL` is intentionally empty until Modal emits the deployed URL.

The current external blocker is HTTPS reachability. Local DNS returned `198.18.0.171` and a TLS
ClientHello received no server response; the independent URL fetch also rejected the address as
non-public. This may be local proxy/fake-IP DNS, but it is not a successful production HTTPS check.
The next window must verify public DNS/TLS from a normal external resolver before sending a live
callback. A development Mac or tunnel must not be substituted.

## Remaining work, in order

1. Confirm `api.bestshiny.com` has genuine public DNS, a valid TLS certificate, and serves the
   BestShiny API containing `/v1/webhooks/character-evidence`. The configured callback is
   `https://api.bestshiny.com/v1/webhooks/character-evidence`.
2. Confirm the Git-ignored `.env` has both Character Evidence keys set without displaying them.
   Keep `CHARACTER_EVIDENCE_OPERATING_MODE=shadow` and threshold version
   `character-evidence-thresholds-2026-08-27-v1`.
3. Confirm Modal Secret `bestshiny-character-evidence-secrets` exists in `uu6first/main`; do not
   regenerate it or desynchronize the shared keys.
4. Rerun the deploy command. Capture the emitted Modal Web Function URL; set that URL as
   `CHARACTER_EVIDENCE_BASE_URL` in the BestShiny production secret store and restart BestShiny.
5. Run a real authorized smoke input through BestShiny. Verify authenticated 202 acceptance, signed
   callback, immutable reference version, all five artifact provenance entries, explicit
   hair/costume `UNAVAILABLE`, and no candidate gate/status change in shadow mode.
6. Inspect Modal logs for model load/warmup and callback delivery. Do not interpret HTTP 202 as a
   production inference pass.
7. The original request requires a real authorized validation set and per-slice metrics, but specifies
   no sample counts. `500 / 25` has been removed. A versioned validation plan must be approved before
   evaluation; until then, do not promote beyond shadow or claim measured accuracy.
8. After live smoke evidence is recorded, update this handoff and the production readiness evidence,
   then commit/open a reviewable PR if requested.

## Verification already completed

```text
.venv/bin/python -m pytest -p no:cacheprovider -q
1084 passed, 9 skipped, 0 failed

Character Evidence targeted regression
12 passed, 0 failed

ruff on every changed Python path
All checks passed

mypy on 18 changed/related source files
Success: no issues found

git diff --check
clean
```

The full pytest run emitted existing deprecation warnings but no failures. A direct invocation of the
pytest entry script initially missed the repository root for existing `scripts.*` imports; rerunning
through `python -m pytest` is the valid result above.

## Working-tree scope

Modified tracked files:

- `.env.example`, `README.md`, `THIRD_PARTY_NOTICES.md`, `pyproject.toml`
- `apps/api/video_platform_api/container.py`
- `apps/api/video_platform_api/main.py`
- `core/qa/qa_core/__init__.py`
- `core/qa/qa_core/evidence.py`
- `core/qa/qa_core/pipeline.py`
- `packages/shared/platform_shared/config.py`
- `HANDOFF.md`

New task-owned paths:

- `config/character-evidence/`
- `services/character-evidence/`
- `tests/test_character_evidence_service.py`
- `docs/CHARACTER_EVIDENCE_HANDOFF_2026-08-28.md`

Do not discard or reset the working tree. Do not modify/delete `.worktrees/` while handling this
handoff.
