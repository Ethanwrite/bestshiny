# Character Evidence on Modal — deployment runbook

First deployed 2026-08-29 into `uu6first/main`. This is the GPU half of the boundary
described in [`CHARACTER_EVIDENCE_HANDOFF_2026-08-28.md`](CHARACTER_EVIDENCE_HANDOFF_2026-08-28.md);
that document explains *what* the pipeline decides and why, and this one is how it gets
deployed and what went wrong the first time it actually was.

```text
BestShiny  →  ModalCharacterEvidenceProducer
           →  POST https://uu6first--bestshiny-character-evidence-https-api.modal.run
                   /v1/character-evidence/analyze      (Bearer, 202 = accepted only)
           →  CVWorker  (T4, min_containers=0, max_containers=1, scaledown 60s)
           →  signed callback → https://api.bestshiny.com/v1/webhooks/character-evidence
           →  shadow-only persistence
```

## 1. What is deployed

| | |
| --- | --- |
| App | `bestshiny-character-evidence` in workspace `uu6first`, environment `main` |
| Endpoint | `https://uu6first--bestshiny-character-evidence-https-api.modal.run` |
| Functions | `https_api` (web), `CVWorker.analyze` (T4 GPU), `redeliver_callbacks` (every 5 min) |
| Secret | `bestshiny-character-evidence-secrets` — API key, callback signing key, callback URL |
| Console | https://modal.com/apps/uu6first/main/deployed/bestshiny-character-evidence |

`min_containers=0` means the GPU is cold by default and costs nothing at rest; the first
request after idle pays a cold start while torch and five model artifacts load.

## 2. Deploying

```bash
cd <repository root>          # paths in the image spec are repo-relative
modal deploy --env main --name bestshiny-character-evidence \
  services/character-evidence/modal_app.py
```

The application code goes in through `add_local_dir`, so it is **mounted at container
start rather than baked into the image**. A code-only change redeploys in seconds and does
not rebuild anything; only a change to the image spec triggers a real build.

Prerequisites, all of which are already true and worth re-checking if a deploy misbehaves:

1. `modal profile current` → `uu6first`.
2. `modal secret list --env main` contains `bestshiny-character-evidence-secrets`. Do not
   regenerate it — the API key and callback signing key are shared with BestShiny's `.env`
   and were written to both in one operation. Regenerating one half silently breaks auth.
3. `api.bestshiny.com` resolves publicly, serves a valid certificate, and exposes
   `/v1/webhooks/character-evidence`. This was the original blocker and is now satisfied by
   the production deployment; see [`DEPLOYMENT.md`](DEPLOYMENT.md).

## 3. Wiring it into BestShiny

On the production host, in `/opt/bestshiny/.env`:

```
CHARACTER_EVIDENCE_BASE_URL=https://uu6first--bestshiny-character-evidence-https-api.modal.run
CHARACTER_EVIDENCE_ENABLED=true
CHARACTER_EVIDENCE_OPERATING_MODE=shadow
```

then `docker compose -f docker-compose.prod.yml up -d --force-recreate api worker`.

Production DI fails closed on a missing or non-HTTPS endpoint, weak keys, or a mode other
than shadow, so a bad value stops the API from starting rather than degrading quietly.
`CHARACTER_EVIDENCE_ENABLED=false` remains the documented way to keep production startable
while the Modal half is unavailable.

**Do not promote past `shadow`.** A versioned validation plan has to be approved first, and
none has been. Shadow records observations without touching the candidate gate.

## 4. Five defects the first real deployment found

The Modal half had been written, reviewed and tested offline, and had never been deployed.
Every one of these was invisible until the deploy actually ran, which is the general lesson:
an image spec is not verified by review.

1. **YOLOX could not install.** Its `setup.py` imports torch at build time, and PEP 517
   build isolation hides the torch installed one layer above. Neither
   `--no-build-isolation` nor `PIP_NO_BUILD_ISOLATION=1` fixes it, because pip's legacy
   editable path hands off to `setup.py develop`, which re-invokes
   `pip install -e . --use-pep517` in a fresh process that rebuilds the isolated
   environment. A **non-editable** install calls the build backend in place, where torch
   already is. `PYTHONPATH` still puts `/opt/YOLOX` first, so imports resolve to the pinned
   source tree either way.
2. **`gh release download` needs a token even for a public repository.** It exited 4 asking
   for `gh auth login`, which a build container cannot answer. Replaced with `curl` against
   the public release URL. Safe because the pinned SHA-256 on the next line is what actually
   establishes the artifact's identity — a wrong URL fails the checksum.
3. **`git clone` of `opencv_zoo` exceeded the repository's LFS budget.** A plain clone
   smudges every model in the zoo, hundreds of megabytes this image never loads, and the
   transfer fails with `This repository exceeded its LFS budget` — an upstream account quota
   nothing here can fix. Even a targeted `git lfs pull` fails against it. The two files are
   fetched from GitHub's media endpoint at the same pinned commit instead, and their bytes
   were compared against the pinned SHA-256 values before the change. Content-addressed
   artifacts are what makes swapping a transport checkable rather than a leap of faith.
4. **`.env()` after `add_local_*` is rejected.** Modal refuses an image that runs a build
   step after local files are added, because those are mounted at start rather than baked
   in. Moving `.env()` above the two `add_local_*` calls is the whole fix.
5. **The detector was written against a newer YOLOX than the manifest pins.** At
   `e1052df7` (0.1.1rc0), `ValTransform.__init__` has no `legacy` argument, `postprocess`
   has no `class_agnostic`, and `get_exp(exp_file, exp_name)` takes both positionally rather
   than as defaulted keywords. All three raised `TypeError` at model load, so every job died
   in `@modal.enter()` before touching a frame — and they surfaced one at a time, each
   needing its own deploy, because the first exception hides the next. Aligned to the pinned
   API, and none of it alters behaviour in a way that matters: `ValTransform()` there defaults to
   `rgb_means=None, std=None`, which is the no-normalisation path `legacy=False` selects in
   the newer API; and the pinned `postprocess` always runs per-class `batched_nms`, which is
   the safer choice here anyway, since class-agnostic NMS can suppress a real person box that
   overlaps a higher-scoring non-person detection, and the tracker never recovers a person it
   was not given.

## 5. What is verified, and what is not

Verified on 2026-08-29:

- The image builds, and all five pinned artifacts pass their SHA-256 checks during the
  build: YOLOX-s, ByteTrack's `byte_tracker.py`, YuNet, SFace, DINOv2-base.
- A build-time `import torch, yolox` assertion passes, so a broken install fails the build
  rather than the first request.
- The endpoint is reachable from the production host.
- Auth is enforced: a well-formed request with no token, and one with a wrong token, both
  get `401 invalid bearer token`.
- An authenticated request with BestShiny's own key returns `202 ACCEPTED`, which proves the
  key in `/opt/bestshiny/.env` and the Modal Secret are the same key.
- BestShiny starts with `CHARACTER_EVIDENCE_ENABLED=true`, so the production fail-closed
  checks on endpoint, keys and mode all pass against the real values.

Not verified, and not to be claimed:

- **No real inference on real media has run.** The probes above carry a deliberately
  unreachable `video_url`; they exercise submission, auth and job claiming, not the pipeline.
- **No signed callback has been observed arriving at BestShiny.** The delivery path,
  including the outbox and its five-minute redelivery schedule, is code that has not yet been
  seen to work end to end.
- **A 202 is acceptance, never evidence.** The handoff says this and it stays true: the
  response means the job identity was claimed and a worker spawned, nothing about the result.
- No accuracy, bias or failure-mode review has been done, and no per-slice metrics exist.
  `SHADOW` is the only defensible mode until a versioned validation plan is approved.

## 6. Operating it

```bash
modal app logs bestshiny-character-evidence --env main     # worker + web logs
modal app list --env main                                  # deployment state
```

A job identity is claimed with `Dict.put(skip_if_exists=True)`, so re-POSTing the same
`job_id` is acknowledged with `duplicate: true` and does not spawn a second GPU worker. That
also means a `job_id` used once cannot be reused — probes need a fresh id each time.

Callbacks that BestShiny does not acknowledge go to a Modal Queue and are retried by
`redeliver_callbacks` every five minutes, up to 60 attempts, after which they move to a
`dead` partition that stays readable for the partition TTL of seven days rather than
disappearing.
