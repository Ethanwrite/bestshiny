# Modal Character Evidence

This service is the production CV boundary for BestShiny. It exposes exactly one public route,
`POST /v1/character-evidence/analyze`, returns `202 ACCEPTED`, and completes by signed asynchronous
callback. HTTP acceptance is not evidence. Production BestShiny has no local inference fallback.

The T4 worker loads one model set per warm container and reuses it: YOLOX-s 0.1.1rc0, ByteTrack,
YuNet 2026may, SFace 2021dec after YuNet five-point alignment, and DINOv2-base. Source commits,
artifact hashes, licenses, pipeline version, and threshold version are pinned in
`character_evidence_model_manifest.json`; the image build verifies every hash before deployment.

## Runtime contract

BestShiny sends short-lived HTTPS URLs for the candidate video and immutable-versioned canonical
references. Modal never receives arbitrary local paths or uploads from the adapter. Each sample
records model versions, threshold version, reference asset/version, pipeline version, tracking
confidence, and quality. Ambiguous crossing, insufficient aligned faces, conflicting identity and
appearance, and gray-zone scores produce `ABSTAIN`, never `PASS`.

All output currently runs in `SHADOW`. The signed BestShiny callback stores observations without
changing candidate gates. Hair and costume are explicitly `UNAVAILABLE` because this model set does
not supply dedicated evidence for either dimension.

## Required configuration

The Modal `main` environment must contain a Secret named
`bestshiny-character-evidence-secrets` with:

- `CHARACTER_EVIDENCE_API_KEY` — high-entropy bearer key, shared with BestShiny.
- `CHARACTER_EVIDENCE_CALLBACK_SIGNING_KEY` — high-entropy HMAC key, shared with BestShiny.
- `CHARACTER_EVIDENCE_CALLBACK_URL` — public HTTPS BestShiny route ending in
  `/v1/webhooks/character-evidence`.

BestShiny production must set the corresponding base URL and keys shown in `.env.example`. Startup
fails closed when the endpoint is not HTTPS, either key is weak/missing, or operating mode is not
`shadow`.

Deploy from the repository root with:

```bash
modal deploy --env main --name bestshiny-character-evidence services/character-evidence/modal_app.py
```

Promotion beyond shadow requires a real authorized dataset satisfying `validation/dataset.schema.json`
and every global/per-slice gate in `config/character-evidence/acceptance-criteria-v1.json`.
