# Production deployment — bestshiny.com

First deployed 2026-08-29 from `claude/rc-predeploy-integration`. This is the runbook
for the host that serves `bestshiny.com`; `docker-compose.yml` in the repository root is
a *local* production-shaped stack and is not what runs there.

## 1. The host

| | |
| --- | --- |
| Address | `153.75.95.10` (Ubuntu 24.04.3 LTS, 2 vCPU, 3.9 GB RAM, 116 GB disk) |
| Application root | `/opt/bestshiny` |
| Compose file | `/opt/bestshiny/docker-compose.prod.yml` |
| Environment | `/opt/bestshiny/.env`, mode `0600`, never in version control |
| DNS | `bestshiny.com`, `www.bestshiny.com`, `api.bestshiny.com` → `153.75.95.10` |

There is no wildcard DNS record. Those three names are the entire public surface.

4 GB of swap was added at `/swapfile`. The image build peaks well above the 3.9 GB of
RAM this host has, and without swap it is the build that dies, not the application.

## 2. Topology

```text
                      ┌─ :80  → 301 to https, plus the ACME challenge root
   Internet ──────────┤
                      └─ :443 ── nginx (host, TLS terminates here)
                                   │
      bestshiny.com ───────────────┼──→ 127.0.0.1:3000   web      (nginx + built SPA)
      www.bestshiny.com ───────────┘
                                   │
      api.bestshiny.com ───────────┴──→ 127.0.0.1:8080   api      (uvicorn)

   internal only, no published port:   postgres (pgvector/pgvector:pg17)
   no port at all:                     worker

   media plane, off-host:              Alibaba OSS  bestshiny-prod-assets-hk
                                       (s3.oss-cn-hongkong.aliyuncs.com)
```

Every application port binds to `127.0.0.1`. The host nginx is the only public
listener, so nothing is reachable except through TLS. This is the main difference
from the repository compose file, which publishes `3000`, `8080` and `5432` on all
interfaces for host-side development.

PostgreSQL publishes **no** port. The repository file publishes `5432` so host-side
`alembic` and the PostgreSQL half of the test matrix can reach the same engine; neither
is run on this host, and `docker compose exec postgres psql` reaches it when needed.

The web container proxies `/api/` to the api container with the prefix **stripped**.
That is deliberate and the frontend depends on it: `apps/web/app.js` sets
`API = "/api"` and then requests paths that carry their own prefix, so the browser asks
for `/api/api/auth/me` and `/api/v1/projects`, and the api container receives
`/api/auth/me` and `/v1/projects`. A bare `https://bestshiny.com/api/auth/register` is
*supposed* to 404 — it is not the URL the application builds.

## 3. Object storage

The media plane is the operator's Alibaba OSS bucket — the one the repository compose
file has always described as the only durable one:

```
S3_ENDPOINT_URL=https://s3.oss-cn-hongkong.aliyuncs.com
S3_REGION=cn-hongkong
S3_BUCKET=bestshiny-prod-assets-hk
S3_ADDRESSING_STYLE=virtual        # OSS is virtual-hosted; "auto" guesses wrong often enough
```

Before this existed, `S3_*` was unset and that was the binding constraint on the whole
reference-media plane: `POST /v1/assets/uploads` answered `501`, and every
reference-carrying shot and every image edit failed closed on
`PROVIDER_REFERENCE_URL_UNAVAILABLE`.

> **A local MinIO briefly filled this role** on the morning of 2026-08-29, before the OSS
> credentials existed, served under `https://api.bestshiny.com/bestshiny-media` because
> there is no wildcard DNS record to give a storage host a name of its own. It was retired
> the same day with nothing stored in it. If a self-hosted store is ever wanted again, the
> constraint that shaped it is worth keeping: SigV4 signs the path **and** the Host header,
> so such a route has to pass both through unrewritten or every presigned URL breaks — and
> the failure reads as an authentication problem rather than a proxy one.

**CORS.** A bucket with no CORS rule answers the browser preflight with `403`, and direct
upload — the whole point of taking the API out of the media path — silently stops working.
The rule is derived from `WEB_ORIGINS` rather than from a wildcard:

```bash
docker compose -f docker-compose.prod.yml exec -T api \
  python scripts/configure_object_storage_cors.py          # plan; --apply to write
```

`PutBucketCors` **replaces** the configuration rather than merging it, so pass the union
of every origin that needs the bucket. This one carries the two production origins and the
two development ones (`http://localhost:3000`, `http://127.0.0.1:18081`) for exactly that
reason: applying production alone would have silently broken local development uploads.

Verify the whole plane — addressing, presigned PUT, checksum binding, CORS, range GET
and the reference URL — with the script that exists for it:

```bash
docker compose -f docker-compose.prod.yml exec -T api python scripts/verify_object_storage.py
```

It reports one standing `WARN` on this bucket: OSS accepts bytes whose SHA-256 does not
match the one bound into the presigned PUT. That is a property of OSS, not a defect here,
and it is why `S3_VERIFY_UPLOAD_SHA256_ON_COMPLETE=true` matters — the completion check is
what actually rejects a mismatched object, and the same run proves that it does.

## 4. Deploying a new revision

The tree is shipped as a `git archive` of the commit being deployed, so what runs is
exactly a commit and never a dirty working tree.

```bash
# from the worktree holding the revision to deploy
SHA=$(git rev-parse HEAD)
git archive --format=tar "$SHA" | gzip -9 > /tmp/bestshiny.tar.gz
scp -o BindInterface=en0 /tmp/bestshiny.tar.gz root@153.75.95.10:/opt/bestshiny/
ssh -B en0 root@153.75.95.10 "
  cd /opt/bestshiny &&
  cp -a docker-compose.prod.yml docker-compose.prod.yml.bak-\$(date +%Y%m%d-%H%M%S) &&
  /usr/local/bin/bestshiny-backup &&
  tar xzf bestshiny.tar.gz && rm bestshiny.tar.gz &&
  echo $SHA > DEPLOYED_SHA &&
  docker compose -f docker-compose.prod.yml build api worker web &&
  docker compose -f docker-compose.prod.yml up -d"
```

**`.env` survives the extraction. `docker-compose.prod.yml` does not.** The first is
gitignored and genuinely absent from the archive; the second is *tracked*, there is no
`.gitattributes` to `export-ignore` it, and `git archive HEAD | tar tf -` lists it — so
extraction overwrites the production copy with the repo's. This document previously
claimed both were safe, which was wrong about the one that carries the compose topology.
Verify before believing either:

```bash
git archive --format=tar HEAD | tar tf - | grep -E '^(docker-compose|\.env)'
```

Hence the `cp -a` above: take the copy before extracting, and diff after. Any hand-edit
made on the host — the kind that gets made during an incident and never gets back into
the repo — is otherwise reverted silently, and the deploy reports success.

`echo $SHA > DEPLOYED_SHA` exists because **the extracted tree carries no `.git`**, so
there is nothing on the host that says which commit is running. Without the marker the
only way to answer "what is deployed?" is to hash files and bisect against candidate
commits — which is how the 2026-08-30 deploy discovered production was on `4832066`
while every record said `9eb2934` (see §6).

The api container runs `alembic upgrade head` before uvicorn, so migrations apply on
start. Startup refuses to run against a database that is not at
`REQUIRED_SCHEMA_REVISION`, and names the command when it does. Take the `pg_dump`
before extracting rather than after: once the new tree is in place, the next `up -d`
migrates, and the backup you want is the one from before that.

**This is not a zero-downtime deploy.** `up -d` recreates the api container, and nginx
answers `502` for the few seconds it is gone — on 2026-08-30 a real session polling a
DePay checkout hit exactly that window. The gap is seconds, but it is user-visible, so
deploy when the site is quiet rather than assuming nobody is mid-flow.

Watch it come up:

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=40 api
docker compose -f docker-compose.prod.yml exec -T api alembic current
```

## 5. TLS

One ECDSA certificate from Let's Encrypt covers all three names, issued with `certonly
--webroot -w /var/www/certbot`. Certbot's systemd timer renews it.

`/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh` reloads nginx after a renewal.
Without that hook nginx keeps serving the expired certificate from memory until
something else happens to reload it — the renewal succeeds and the site still breaks.

## 6. Operational state

- **Current release.** `c91114b` (`main`, the squash of
  [#68](https://github.com/Ethanwrite/bestshiny/pull/68) — the appended negative-prompt guards are joined
  without a full stop in front of them), merged 2026-09-10 ≈15:51Z and deployed ≈15:54Z on the operator's
  "deploy it once both are green". `DEPLOYED_SHA.prev = 064a577`. **No migration** (`alembic current` stayed
  `0082_shot_cinematography_plan (head)`), no `.env` change, `COMPOSE_UNCHANGED`, archive SHA-256
  `aea03358…` compared on both ends, `DEPLOY_EXIT=0`. Gated on the exact tree that shipped (squash tree
  `d4701f0b…` equals the gated branch tip's): SQLite 1843 passed / 20 skipped, PostgreSQL 1856 passed /
  7 skipped, both exit 0, ruff and mypy clean. Three files against `064a577`.

  The cosmetic residual recorded under `064a577`: a Skill writes its negative prompt as a sentence about as
  often as a list, and appending the baseline guards to `…inconsistent night rooftop setting.` produced
  `…setting., visual style drift, …`. Terminal punctuation is now dropped from the Skill's own text only
  when something is actually being appended (`. , ; :` and `。，、；：`); when the Skill already named every
  guard its text goes out untouched, where a trailing comma used to be stripped for nothing.

  Verified on the deployed image against the real stored package: the provider now receives
  `…out of frame city skyline, inconsistent night rooftop setting, visual style drift, palette drift, …`
  with no baseline guard absent. All three running image IDs equal the freshly built ones (api
  `e69a5e6b58a2`, worker `270e108f329c`, web `1eb8ba7d93bf`), restarts 0, served bundle unchanged
  (`/assets/index-B0M_LxJ6.js`), local and public 200 on all three paths, zero tracebacks. `web` needed
  `up -d --force-recreate web` for the third deploy running — treat it as part of the procedure, not an
  anomaly.

- **Previous release.** `064a577` (`main`, the squash of
  [#67](https://github.com/Ethanwrite/bestshiny/pull/67) — the compiler Skill's package ships when it
  renders the action instead of quoting it), merged 2026-09-10 ≈14:55Z and deployed ≈14:58Z on the
  operator's "ship it once postgres is green". `DEPLOYED_SHA.prev = 5c89736`. **No migration** (`alembic
  current` stayed `0082_shot_cinematography_plan (head)`), no `.env` change, `COMPOSE_UNCHANGED`, archive
  SHA-256 `957e2882…` compared on both ends, `DEPLOY_EXIT=0`. Gated on the exact tree that shipped (the
  squash tree hash equals the gated branch tip's, `e0018f25…`): SQLite 1843 passed / 20 skipped,
  PostgreSQL 1856 passed / 7 skipped, both exit 0, ruff and mypy clean, `review_skill_contract.py` PASS.

  **Why.** The `5c89736` live check was discarded with `dominant_action_missing`: the model rendered
  `雨桐进入城市天台上发现了一部不属于她的手机` as `雨桐进入城市天台，发现了一部不属于她的手机。` — one particle
  fewer, one comma more — and the exact-substring test made that a rewrite, so the deterministic JSON dump
  went to the provider instead of the paid wording. `action_preserved` now accepts an exact quotation, a run
  of the action's own tokens (only punctuation or spacing differed), or a package carrying at least 80% of
  the action's adjacent-token pairs; tokens are words in scripts that separate them and characters in
  scripts that do not. A package that renders rather than quotes ships with `ACTION_PARAPHRASED` recorded.

  **Live check after the deploy, 2026-09-10 ≈15:02Z, USD 0.0016** (one `PROMPT_COMPILER` call on the same
  E2E-audit shot that had failed): `status COMPILED, execution_mode MODEL, skill_driven True`, no fallback,
  reason codes `SKILL_LOADED, MODEL_REPLY`. **The paid wording is what the provider receives** — the
  delivered prompt is the Skill's prose, not the JSON dump, with the Seedance adapter's own line after it.
  This run happened to quote the action exactly, so the strict rule would also have passed it; what the
  change buys is that the *class* of reply which renders the action no longer loses its package. The
  negative prompt merge was verified on the real package: the Skill named identity drift itself and the
  remaining baseline guards were appended, none absent. `web` again needed
  `up -d --force-recreate web` (same systematic behaviour as `5c89736`; served bundle byte-identical,
  `/assets/index-B0M_LxJ6.js`). Zero tracebacks, restarts 0, local and public 200.

  **One cosmetic residual:** the merge joins the Skill's sentence-terminated negative prompt to the appended
  guards with a comma, so the string reads `… night rooftop setting., visual style drift, …`. Harmless to a
  renderer, ugly in a record; the joiner strips a trailing comma but not a trailing full stop.

- **Previous release.** `5c89736` (`main`, the squash of
  [#66](https://github.com/Ethanwrite/bestshiny/pull/66) — the Skill runtime's decisions reach the output:
  the prompt-compiler Skill's package is the body every adapter delivers (`AdapterInput.package`, with the
  locked style, the unrestated continuity assertions and constraints and the bounded context after it, and
  the shot's prohibitions merged into the negative prompt); a Skill-driven continuity `ESCALATE` blocks the
  shot's compilation and generation until a real user acknowledges it
  (`POST /v1/shots/{id}/continuity/review/acknowledge`, `GET …/continuity/review`, 409
  `CONTINUITY_APPROVAL_REQUIRED`), while deterministic reviews stay advisory; the compiler's preflight
  refuses markers and hedges in every photographic field and the cinematography plan's own `unresolved`
  entries; the whole cinematography design reaches the shot spec and every prompt surface; package freshness
  keys on the producing Skill's content hash; entry point `docs/SKILL_RUNTIME.md` §10, ledger
  `docs/OPEN_ISSUES.md` §2.53), merged 2026-09-10 ≈12:44Z and deployed ≈12:48Z on the operator's "merge the
  PR and deploy once postgres is green". `DEPLOYED_SHA.prev = 4cf175b`. **No migration** (`alembic current`
  stayed `0082_shot_cinematography_plan (head)`), no `.env` change, `COMPOSE_UNCHANGED` (byte-equal to
  `docker-compose.prod.yml.bak-20260910-124704`, taken before extraction). Gated before the merge on both
  engines on the exact tree that shipped — the squash commit's tree hash equals the gated branch tip's
  (`ab04eff9…`): SQLite 1840 passed / 20 skipped, PostgreSQL 1853 passed / 7 skipped, both exit 0, ruff and
  mypy clean, `review_skill_contract.py` 3 × PASS, plus a five-reviewer adversarial review of the diff whose
  three confirmed findings and ten corrections are in the branch. Delta against the running `4cf175b`: 29
  files, no `migrations/`, no `apps/web/`, `REQUIRED_SCHEMA_REVISION` unchanged; the archive's SHA-256
  (`7bbab0dc…`) was compared on both ends before extraction (`deploy-5c89736.log`, `DEPLOY_EXIT=0`, about
  two minutes).

  Verified after (`verify_remote_66.sh` on the host): markers written (`5c89736`, `.prev` =
  `4cf175ba1d45…`), api healthy on the first check, `RestartCount=0` on all three, local 8080/3000 200 and
  public `https://api.bestshiny.com/health`, `https://bestshiny.com/app`,
  `https://bestshiny.com/api/health` all 200. Inside the running api image the release's own behaviour was
  exercised, not merely imported: `AdapterInput.package` exists, `prompt_lines` returns the package as the
  body, a `must not show or do:` constraint is delivered and merged into the negative prompt beside the
  baseline guards, `ContinuityReviewer` carries `pending_escalation` / `ensure_reviewed` / `acknowledge` /
  `gate_view` with `CONTINUITY_ESCALATION_ACKNOWLEDGED` and `CONTINUITY_APPROVAL_REQUIRED`, `_preflight`
  refuses `['atmosphere', 'camera.framing']` on a TBD / "provisional" spec, the camera design fields and
  `atmosphere` / `composition` are on the spec, freshness compares the producing hash, and both new routes
  are registered. `SkillRuntime.validate()` empty, five Skills integrated, and the three edited bodies are
  the ones running (`prompt-compiler sha256:eab237b8a3c8feab`, `continuity sha256:28480306a9819c03`,
  `cinematography sha256:f04d93bb07befd85`). Zero tracebacks in api or worker. Data untouched (5 projects,
  29 generation jobs, 23 prompt compilations, 194 decision records, 16 media assets). Disk 13% used.

  **The web container needed an explicit recreate.** `up -d` rebuilt all three images and recreated api and
  worker, but left `web` on the previous image (`188db9723ef9`) while the `bestshiny-web:latest` tag had
  moved to the freshly built `2aac31a24f13` — so "what runs" briefly disagreed with "what was deployed".
  This release touches no `apps/web/` file and no web build input, so the new image differs only because the
  build context (the whole repository) is part of a `COPY` layer; a
  `docker compose -f docker-compose.prod.yml up -d --force-recreate web` settled it, and the served bundle
  is byte-identical before and after (`/assets/index-B0M_LxJ6.js`, the same hash as `1494e72` and
  `74beba5`). Worth checking on every deploy: compare each running image ID against its freshly built one
  rather than trusting `up -d` to have recreated everything.

  **Known effect worth watching** (measured after the deploy, and smaller here than the general case):
  production held **no** Skill-compiled packages at all — all 23 `prompt_compilations` rows are
  image-prompt-corrector records with `execution_mode` unset — so nothing was invalidated and the "one paid
  compile per shot" cost is simply the first compile each shot was always going to pay. Where such packages
  do exist, they are stale at once, because the new spec fields (`camera.height` / `lens_intent` / `depth_of_field`, `lighting.motivation` /
  `exposure_intent`, `atmosphere`, `composition`) change every envelope hash, and the three edited Skill
  bodies change their content hashes. Under `FEATURE_SKILL_STAGES_AT_APPROVAL` (unset, so the code default
  *on* applies) a shot's next generate pays one prompt-compiler call; a synchronous compile before that is
  the deterministic package on record (`NO_FRESH_SKILL_COMPILATION`, plus `SKILL_VERSION_CHANGED:<old>`);
  and a same-key generate straddling the deploy is a 409 rather than a replay, because
  `metadata.canonical_shot_spec` is inside the gateway's idempotency hash. The cinematography reuse key
  moved with it (the stage's own `shot_type` write-back left the key), so the first re-run of that stage per
  shot re-designs rather than reusing. A shot whose previous shot has no registered `END_FRAME` will now meet
  the continuity gate as a 409 naming the decision, where before the same verdict was only recorded.
  Rolling back to `4cf175b` needs no downgrade (same schema); prefer the `bak-20260910-124704` compose copy
  and the pre-extraction database backup.

  **Live check on production, 2026-09-10 ≈13:53Z, USD 0.0041.** Three paid calls on the platform's own
  `E2E 短剧 audit` project (the `e2e-free-audit-0830@bestshiny.com` test account; no real user's project was
  touched), all answered by the FREE binding `doubao-seed-2-0-lite-260428` under the USD 200/day breaker.
  What it proved, on real production rows: **cinematography** ran Skill-driven (`SKILL_LOADED, MODEL_REPLY`)
  and its whole design reached the shot spec — `camera.height` "2.2 meters above rooftop concrete surface",
  a lens intent, a depth of field and an atmosphere, and `subject_positions` moved 雨桐 from the timeline's
  `midground_center` to `screen left, midground` with the plan's stray "phone" entry recorded as
  `unmatched_subject_positions` (gap 4, live); **continuity** ran Skill-driven and returned `ESCALATE` with
  `approval_required` on the second shot — no registered `END_FRAME` for a `CONTINUOUS` transition, zero
  mismatches — and the gate closed on that shot for the first time in production (decision `e8442e9e`,
  gap 2, live); **prompt compilation** ran, and the model's package was **discarded** (below).

  **Residual the live check exposed — `dominant_action_missing`.** The compiler Skill answered, but the
  runtime's re-verification rejected its package because the shot's `dominant_action`
  ("雨桐进入城市天台上发现了一部不属于她的手机") did not appear as an exact casefolded substring of the model's
  positive prompt, so the deterministic JSON package stood in (`MODEL_OUTPUT_INVALID`, on record) and the
  adapter delivered the canonical rendering rather than the paid wording. The delivery path built in this
  release is therefore correct but unexercised in production: it only carries a package that survives
  re-verification. The check is pre-existing (`verify_compiled_package`, shipped in `1494e72`/#64) and is
  there so the compiler cannot quietly drop or reword the action; the open question is whether a whole
  Chinese sentence is the right unit for a verbatim substring test, or whether it should compare
  punctuation- and whitespace-normalised content. **Not changed here** — loosening it is a contract decision
  about how much rewording the compiler may do, and it wants its own review. Recorded as
  `docs/OPEN_ISSUES.md` §2.53's residual.

  One shot of the E2E project (`ded8a661`) is left blocked by the gate, which is the correct outcome of a
  real `ESCALATE` and is also the first end-to-end proof that the gate works in production. Clearing it
  needs `POST /v1/shots/ded8a661…/continuity/review/acknowledge` from a real user, or a registered
  `END_FRAME` on the previous shot and a re-review.

- **Previous release.** `74beba5` (`main`, the squash of
  [#65](https://github.com/Ethanwrite/bestshiny/pull/65) — the Skill contracts tolerate live replies:
  a Shot Planner micro-action outside the closed vocabulary, a malformed `micro_actions` field or a
  fifth canonical key is dropped and recorded (`SHOT_PLANNER:MICRO_ACTION_DROPPED:<value>`) instead of
  failing the whole plan; the cinematography string limits bound prompt size (focus / path / position /
  lens intent 400, compositions 800); a script-compiled first shot takes its actor and the acted-upon
  prop from its output state with provenance on the compilation record only; the story and shot-plan
  calls get 12k / 16k output caps and the runtime records `MODEL_OUTPUT_TRUNCATED` when the provider
  stops at the cap; entry point `docs/SKILL_RUNTIME.md` §8-§9, ledger `docs/OPEN_ISSUES.md` §2.52),
  merged 2026-09-09 ≈12:07Z and deployed ≈12:12Z on the operator's "commit this, open a PR, merge and
  deploy". `DEPLOYED_SHA.prev = 1494e72`. **No migration** (`alembic current` stayed
  `0082_shot_cinematography_plan (head)`; the explicit upgrade on the new image was a no-op), no `.env`
  change, `COMPOSE_UNCHANGED`. Same ordering as the `1494e72` deploy (`deploy_remote.sh` rewritten for
  this SHA, log `deploy-74beba5.log`, `DEPLOY_EXIT=0`, api healthy on the first check, about 2 minutes).
  Gated before the merge on both engines: SQLite 1822 passed, PostgreSQL 1835 passed, ruff and mypy
  clean, plus a three-lens adversarial review of the diff. Delta against the running `1494e72`:
  `git diff --name-status 1494e72 74beba5` = the PR's 11 code/test files plus the two release-record
  commits' docs; the archive's SHA-256 (`717943df…`) was compared on both ends before extraction.

  Verified after (`verify_remote_65.sh` on the host): both markers written (`DEPLOYED_SHA` = the full
  `74beba5e…`, `.prev` = `1494e72a…`), the compose file byte-equal to the `bak-20260909-121048` copy,
  all three running image IDs equal the freshly built ones (api `aa6156323207`, worker `df95b937a131`,
  web `3e5cc2b9e2bd`), `RestartCount=0` on all three, api healthy, local 8080/3000 200, public
  `https://api.bestshiny.com/health`, `https://bestshiny.com/app` and `https://bestshiny.com/api/health`
  all 200 from the host. Inside the api image `STORY_MAX_OUTPUT_TOKENS = 12000`,
  `SHOT_PLAN_MAX_OUTPUT_TOKENS = 16000`, `MODEL_OUTPUT_TRUNCATED` importable, `CinematographyCamera.focus`
  capped at 400, `SkillRuntime.validate()` empty and five Skills integrated. The web service was rebuilt
  but untouched by the release: the served bundle is byte-identical (`/assets/index-B0M_LxJ6.js`, the same
  hash as `1494e72`). Zero tracebacks in api or worker. Data untouched (14 sessions, 6 anchors, 29 jobs —
  11 COMPLETED / 7 FAILED / 1 RETRY_WAIT live, 10 deleted — 16 READY media assets, 0 screenplay rows,
  23 prompt compilations, 194 decision records, `memory_index_outbox` empty). Disk 12% used.

  **Known effect worth watching:** a prompt-compiler package recorded before this release for a
  script-compiled first shot no longer matches that shot's envelope hash (the cast is now part of it), so
  its first generation compiles deterministically on record (`NO_FRESH_SKILL_COMPILATION`) until the
  compile stage reruns. Rolling back to `1494e72` needs no downgrade (same schema); prefer the
  `bak-20260909-121048` backups.

  **Redeployed ≈12:58Z as `4cf175b`** — `main`'s head at the time, which is `74beba5` plus this release
  record (four documentation files, nothing that runs) — on the operator's "deploy the docs commit so
  the marker matches main". `DEPLOYED_SHA.prev = 74beba5`, no migration, no `.env` change,
  `COMPOSE_UNCHANGED`, `deploy-4cf175b.log`, `DEPLOY_EXIT=0` (the pip layer was rebuilt from scratch
  this time, about 4 minutes). Behaviour-neutral by construction and verified so: all three running
  image IDs equal the freshly built ones (api `8981137f6994`, worker `fe7f1728669c`, web
  `188db9723ef9`), restarts 0, api healthy on the first check, local and public 200 on all three
  paths, the same caps and codes importable, five Skills integrated, the served bundle byte-identical
  (`/assets/index-B0M_LxJ6.js`), zero tracebacks, data untouched. Recording this redeploy is itself a
  docs commit, so `main` is again one docs commit ahead of the marker - the runbook's steady state.

- **Previous release.** `1494e72` (`main`, the squash of
  [#64](https://github.com/Ethanwrite/bestshiny/pull/64) — the Skill runtime: one operation resolves to
  one Skill from machine-readable frontmatter, that body is injected alone under the Skill's model role,
  every row records `resolved / loaded / model_invoked / execution_mode / fallback_reason`; five Skills
  integrated (director, short-drama as the new `SHOT_PLANNER` role, cinematography, continuity,
  prompt-compiler), the screenplay written as a Director story plus a Shot Planner plan, cinematography
  design / continuity review / Skill-backed prompt compilation over every compiled episode, invariant
  versioning on every revision; entry point `docs/SKILL_RUNTIME.md`, ledger entry `docs/OPEN_ISSUES.md`
  §2.52), merged 2026-09-09 ≈10:01Z and deployed ≈10:12Z on the operator's "merge it and deploy to
  production". `DEPLOYED_SHA.prev = 60d7764`. **One migration, `0081` → `0082_shot_cinematography_plan`**
  — a plain `ADD COLUMN shots.cinematography_json JSON NOT NULL DEFAULT '{}'` (the migration's target
  was read first: 8 shots, column absent, 0 `SHOT_PLANNER` bindings), run explicitly on the new image
  after `stop worker` and before `up -d api web`, `alembic current` = `0082_shot_cinematography_plan
  (head)` afterwards. No `.env` change: `FEATURE_SKILL_STAGES_AT_APPROVAL` is unset, so the code default
  *on* applies — under `PROVIDER_MODE=live` a beats approval now pays about 2N+1 small text calls per
  N-shot episode (cinematography per shot, continuity per adjacent pair, prompt compilation per shot)
  and a generation request pays one prompt-compiler call when no fresh package exists for its exact
  envelope (a replay or an identical binding set reuses it). `COMPOSE_UNCHANGED`. Same ordering as the
  `60d7764` deploy (`deploy_remote.sh` rewritten for this SHA, log `deploy-1494e72.log`, `DEPLOY_EXIT=0`,
  api healthy on the first check, about 2.5 minutes including the image build). Delta against the running
  `60d7764`: `git diff --name-status 60d7764 1494e72` = the PR's 54 files plus the two release-record
  commits' `docs/DEPLOYMENT.md` / `HANDOFF.md`; the archive's SHA-256 (`9c68fbba…`) was compared on both
  ends before extraction.

  Verified after (`verify_remote.sh` on the host): both markers written (`DEPLOYED_SHA` = the full
  `1494e72ac…`, `.prev` = `60d77644…`), the compose file byte-equal to the `bak-20260909-100943` copy,
  all three running image IDs equal the freshly built ones (api `cd99e3799e66`, worker `7c88db322a17`,
  web `6da1c472f351`), `RestartCount=0` on all three, api healthy, local 8080/3000 200, public
  `https://api.bestshiny.com/health`, `https://bestshiny.com/app` and `https://bestshiny.com/api/health`
  all 200 from the host. New contracts answer: `GET /v1/skills`, `GET /v1/skills/runtime`,
  `POST /v1/shots/{id}/cinematography`, `POST /v1/shots/{id}/continuity/review`,
  `POST /v1/shots/{id}/prompt/compile` and `POST /v1/episodes/{id}/skill-stages` are 401
  unauthenticated against a 404 control (routed and auth-gated) and all five new paths are in the
  OpenAPI; inside the api container `SkillRuntime(SkillRegistry(Path("./skills"))).validate()` is empty
  and the matrix reads five bound-and-injected (director, short-drama, cinematography, continuity,
  prompt-compiler) and seven reference Skills. The startup sync inserted the three `SHOT_PLANNER`
  bindings (ALL PRIMARY priority 0, ALL FALLBACK priority 10, FREE PRIMARY priority 0, all enabled);
  `shots.cinematography_json` exists and all 8 shots carry `{}`. The served bundle
  `/assets/index-B0M_LxJ6.js` carries "director + shot planner skills", "Shots by rules",
  `SHOT_PLANNER:SKILL_LOADED`, "runtime-bound", "body injected" and `skill_invocations`. Zero
  tracebacks in api or worker. Data untouched (14 sessions, 6 anchors, 29 jobs — 11 COMPLETED /
  7 FAILED / 1 RETRY_WAIT live, 10 deleted — 16 READY media assets, 0 screenplay rows, 23 prompt
  compilations, 194 decision records, `memory_index_outbox` empty). Disk 12% used.

  **Not verified here: no live model has answered under the new protocols.** The story, shot-plan,
  cinematography, continuity and compiler calls were only ever driven by scripted doubles. After the
  first paid session through beats approval, read `creative_screenplays.reason_codes` (the
  `SHOT_PLANNER:*` codes say whether the planner's plan was accepted or rejected whole), the
  `CINEMATOGRAPHY_DESIGN` and `CONTINUITY_REVIEW` rows in `decision_records`, and
  `prompt_compilations.diff_json.skill_invocation`. Rolling back to `60d7764` needs a downgrade to
  `0081` (that image pins it); `0082`'s `downgrade()` is a plain `DROP COLUMN` and was exercised on a
  scratch database only — prefer the `bak-20260909-100943` backups.

- **Previous release.** `60d7764` (`main`, the squash of
  [#63](https://github.com/Ethanwrite/bestshiny/pull/63) — the third 2026-09-07 production review
  verified and fixed: the Director stage bound to the variant under review, a sequenced project
  switch, deleted creations forgotten everywhere, wallet polling that retries under backoff, parks
  behind "Check payment status again" and ends when the sheet closes, the dead Director controls
  and the criticality picker removed, the plan read from the open project's workspace, paged
  Productions and admin lists, admin deep links and audit rows, refunded-vs-charged, "Try again" on
  the gateway's own `allowed_actions`, the pricing page on the public catalogue, key visuals through
  the thumbnail cache, an admin console that scrolls, IME Enter, a read address for direct uploads,
  the worker busy flag, the web proxy's WebSocket upgrade, the length that runs beside the length
  asked for, no Regenerate on an approved shot; ledger entry `docs/OPEN_ISSUES.md` §2.51), merged
  2026-09-07 ≈11:05Z and deployed ≈11:16Z on the operator's "合并 PR #63，同步生产服务器".
  `DEPLOYED_SHA.prev = 55b4da9`, so the marker is back on a commit of `main`. No migration
  (`alembic current` stayed `0081_veo_discrete_durations (head)`; the explicit upgrade on the new
  image was a no-op), no `.env` change (`PUBLIC_BASE_URL=https://api.bestshiny.com` is what the new
  asset read address is minted from), `COMPOSE_UNCHANGED`. Same ordering as the `55b4da9` deploy
  (`deploy_remote.sh` rewritten for this SHA, log `deploy-60d7764.log`, `DEPLOY_EXIT=0`, api healthy
  on the first check, 97 seconds end to end). Delta against the running `55b4da9`:
  `git diff --name-status 55b4da9 60d7764` = the review's 25 files plus the `0d4e71e` release record;
  the archive's checksum was compared on both ends before extraction.

  Verified after: both markers written (`DEPLOYED_SHA` = the full `60d77644…`, `.prev` = `55b4da94…`),
  the compose file byte-equal to the `bak-20260907-111424` copy, all three running image IDs equal
  the freshly built ones (api `a74aa6d7bdf6`, worker `0c9328dc9b48`, web `ebb3fdc108ee`),
  `RestartCount=0` on all three, api healthy, local 8080/3000 200, public
  `https://api.bestshiny.com/health`, `https://bestshiny.com/app` and the browser's own proxy path
  `https://bestshiny.com/api/health` all 200 from the host. New contracts answer: `GET
  /v1/payments/catalog` 200 without a session and carries the three packs (20/1,800, 50/6,000,
  100/11,000 USDC) while `/v1/payments/config` stays 401; the OpenAPI carries the listing's `before`
  query and the audit log's `id`; `GET /v1/generations?…&before=` is 401 unauthenticated (routed and
  gated). `nginx -T` in the web container shows the `map $http_upgrade $connection_upgrade` block
  and both `proxy_set_header Upgrade` / `Connection $connection_upgrade` lines inside `/api/`, with
  `client_max_body_size 100m` and `proxy_read_timeout 300s` unchanged. The served bundle
  `/assets/index-BmnQfRZz.js` carries `shotStageTake`, `walletRecheckBtn`, `productionsMore`, `Load
  older creations`, `Check payment status again`, `On stage`, `is-staged`, `next_cursor`,
  `allowed_actions`, `requested_duration`, `Approved · in the timeline`, `isComposing`,
  `/v1/payments/catalog` and `data-page-step`, and no longer carries `Regenerate shot`,
  `passengerCriticality` or `cameraScale`; the served `index.html` carries `id="shotStageTake"`,
  `id="walletRecheckBtn"`, `id="productionsMoreBtn"` and "Camera, for the continuity check", and no
  `passengerCriticality`, `cameraScale` or `lighting` control. Zero tracebacks in api or worker.
  Data untouched (14 sessions, 28 jobs in the same four terminal states as before — 3 CANCELLED /
  13 COMPLETED / 8 FAILED / 4 RETRY_WAIT — 0 deleted, 14 READY media assets, `memory_index_outbox`
  empty). Disk 11% used.

  **Host nginx changed with it, ≈11:51Z, on the operator's "加上主机 nginx 的 WebSocket 头".** Neither
  public server block forwarded `Upgrade`/`Connection`, so the worker WebSocket could not upgrade
  through `https://bestshiny.com/api/` (host → web container → api) or `https://api.bestshiny.com`
  (host → api) whatever the containers did. `/etc/nginx/conf.d/websocket-upgrade.conf` now carries
  the `map $http_upgrade $connection_upgrade { default upgrade; '' close; }` block (http context),
  and both `location /` blocks of `/etc/nginx/sites-available/bestshiny` gained
  `proxy_set_header Upgrade $http_upgrade;` and `proxy_set_header Connection $connection_upgrade;`
  after their `proxy_read_timeout 300s` (`.bak-20260907-115129` holds the previous file; `nginx -t`
  clean, graceful reload, site 200, keep-alive still reuses a connection). Proof: an HTTP/1.1
  handshake probe (`Connection: Upgrade`, `Upgrade: websocket`, no credential) on either public path
  now reaches the api as a WebSocket and gets its own refusal — the api logs
  `"WebSocket /v1/workers/ws/probe" 403` from both the web container (`172.18.0.4`) and the host
  (`172.18.0.1`) — where before the same probe arrived as a plain `GET … 404`. The chain is host →
  web container → api, all three forwarding the upgrade. **Probe over HTTP/1.1 only:** the vhosts
  speak HTTP/2, `curl` negotiates it by default, and HTTP/2 has no `Upgrade` header, so the same
  request without `--http1.1` still reads 404 — that is the protocol, not a missing header;
  browsers open `wss://` over HTTP/1.1. The extension's HTTP polling was never affected, and no
  client uses the socket today.

  **Rollback.** No migration, so a code rollback to `55b4da9` is a plain redeploy of that revision;
  the pre-extraction `bestshiny-backup` dump and the `*.bak-20260907-111424` copies of `.env` and the
  compose file remain available.

- **Previous release.** `55b4da9` (branch `claude/payment-nonce-rpc-response-bugs-35c85c`, the head
  of [#62](https://github.com/Ethanwrite/bestshiny/pull/62), open and not merged at deploy time, on
  top of `main` `a1606ed` — the second 2026-09-06 production review verified and fixed: the relayer
  nonce floor, recovery of a lost `/submit` answer, the per-shot spending cap enforced, sign-out
  that reports failure and clears the previous account, the multipart upload off the event loop,
  the wallet bound to the open project's workspace, the inspector toggle under 1280px; ledger entry
  `docs/OPEN_ISSUES.md` §2.50), deployed 2026-09-07 ≈09:29Z. `DEPLOYED_SHA.prev = 57117a2`. No
  migration (`alembic current` stayed `0081_veo_discrete_durations (head)`; the explicit upgrade on
  the new image was a no-op), no `.env` change, `COMPOSE_UNCHANGED`. Same ordering as the `57117a2`
  deploy (`deploy_remote.sh` rewritten for this SHA, log `deploy-55b4da9.log`, `DEPLOY_EXIT=0`, api
  healthy on the first check, ~2 minutes end to end). Delta against the running `57117a2`:
  `git diff --name-status 57117a2 55b4da9` = the review's 22 files plus the `a1606ed` release record.

  Verified after: both markers written (`DEPLOYED_SHA` = the full `55b4da94…`, `.prev` = `57117a2…`),
  all three running image IDs equal the freshly built ones (api `e7feb286ecca`, worker
  `e59e000be6f8`, web `b4dd9f5ee9bf`), `RestartCount=0` on all three, local 8080/3000 200, public
  `https://api.bestshiny.com/health`, `https://bestshiny.com/app` and the browser's own proxy path
  `https://bestshiny.com/api/health` all 200 from the host, `/health` still `auth.password_reset_available:
  false` and both reset endpoints still 503. The api's OpenAPI carries `spend_cap_usd`; an
  unauthenticated multipart `POST /v1/assets` answers `401` against a `404` control (the route is
  registered as a plain `def`), `GET /v1/providers` 401. The served bundle `/assets/index-AF60R-5h.js`
  carries `inspectorToggleBtn`, `walletRecipient`, `spend_cap_usd`, `ai-director:workspace-changed`,
  `Sign-out did not complete`, `do not pay again` and `no new signature is needed`; the served
  `index.html` carries `id="inspectorToggleBtn"`, `id="logoutError"`, `id="walletRecipient"` and the
  cap's "0 means no limit" note. `nginx -T` in the web container still shows `client_max_body_size
  100m` and `proxy_read_timeout 300s`; the host `bestshiny.com` block still `128m` / `300s`. Zero
  tracebacks in api or worker. Data untouched (14 sessions, 6 anchors, 28 jobs in the same four
  terminal states as before the deploy — 3 CANCELLED / 13 COMPLETED / 8 FAILED / 4 RETRY_WAIT —
  14 READY media assets, 8 relayer authorizations — 1 CONFIRMED at nonce 0, 7 EXPIRED without a
  nonce — `memory_index_outbox` empty, 0 cleanup rows, 0 password-reset tokens).

  **Rollback.** No migration, so a code rollback to `57117a2` is a plain redeploy of that revision;
  the pre-extraction `bestshiny-backup` dump and the `*.bak-20260907-092757` copies of `.env` and the
  compose file remain available. Merging #62 later changes nothing on the host: its squash's tree is
  this tree, so `DEPLOYED_SHA` moves to a commit on `main` at the next redeploy, as with #44.

- **Previous release.** `57117a2` (`main`, [#61](https://github.com/Ethanwrite/bestshiny/pull/61)
  — the password-reset flow closed where no token can reach the user, the audit's F13 decision in
  `docs/OPEN_ISSUES.md` §1.19), deployed 2026-09-06 ≈19:01Z. `DEPLOYED_SHA.prev = cc60332`. No
  migration (`alembic current` stayed `0081`), no `.env` change, `COMPOSE_UNCHANGED`. Same
  ordering as the `fe91a8f` deploy (`deploy_remote.sh`, log `deploy-57117a2.log`, `DEPLOY_EXIT=0`;
  api healthy on the first check).

  What changed for users: `AuthService.password_reset_available` is true only where the response
  carries the token (development, test). In production both `/api/auth/password-reset/*` endpoints
  answer `503 密码重置功能暂未开放，请联系支持` and create no token row, `GET /health` reports
  `auth.password_reset_available: false`, and the web app keeps its *Forgot password?* entry hidden
  unless `/health` offers it. A locked-out user needs an operator reset until a delivery channel
  exists.

  Verified after: both markers written, all three running image IDs equal the freshly built ones,
  `RestartCount=0` on all three, local 8080/3000 200, `https://api.bestshiny.com/health` and
  `https://bestshiny.com/api/health` both 200 with `auth.password_reset_available: false`, both
  reset endpoints 503 through the browser's own proxy path (`bestshiny.com/api/api/auth/...`),
  the served `/login` markup carries `id="forgotPasswordBtn" … hidden`, the new bundle
  `/assets/index-CYVUkgMF.js` reads the flag, and a real browser view of `/login` shows only
  *No account? Create one* under the sign-in button (the button's computed display is `none`, zero
  size). Zero tracebacks in api or worker; data untouched (14 sessions, 28 jobs, 6 anchors, 0
  password-reset tokens).

  **Rollback.** No migration, so a code rollback to `cc60332` is a plain redeploy of that
  revision; the pre-extraction `bestshiny-backup` dump and the `*.bak-20260906-185951` copies of
  `.env` and the compose file remain available.

- **Previous release.** `cc60332` (`main`, [#60](https://github.com/Ethanwrite/bestshiny/pull/60)
  — the 2026-09-06 production-workflow audit, verified and fixed: per-upload direct-upload slots
  under OSS's verify mode, relayer nonce and expiry recovery, batch-sibling QA with its restart
  recovery, deferred verification reads, budget settlement and pre-transport recovery, direct-API
  worker isolation, completed-upload replay, the web proxy's read timeout; ledger entry
  `docs/OPEN_ISSUES.md` §2.49), deployed 2026-09-06 ≈18:47Z. `DEPLOYED_SHA.prev = 5010ce5`.
  No migration (`alembic current` stayed `0081`; the explicit upgrade on the new image was a
  no-op), no `.env` change, `COMPOSE_UNCHANGED`. Same ordering as the `fe91a8f` deploy
  (`deploy_remote.sh`, log `deploy-cc60332.log`, `DEPLOY_EXIT=0`; api healthy on the first check).

  Verified after: both markers written, all three running image IDs equal the freshly built ones,
  `RestartCount=0` on all three, local 8080/3000 200, public `api.bestshiny.com/health` and
  `bestshiny.com/app` 200 from the host, `nginx -T` inside the web container shows
  `client_max_body_size 100m` and `proxy_read_timeout 300s`, the new bundle
  `/assets/index-B4W3uWQh.js` keys each upload attempt (`web-upload-${crypto.randomUUID()}`),
  zero tracebacks in api or worker, and data untouched (14 sessions, 28 jobs, 6 anchors, 14 READY
  media assets, `memory_index_outbox` empty). The worker's new start-up recoveries found nothing
  to do: no candidate was stranded in VALIDATING without a verdict, and the four UNCERTAIN spend
  authorizations on this host all belong to jobs that did not complete, so they stay for the
  operator (`docs/OPEN_ISSUES.md` §1.18). All three `direct-api:*` workers READY.

  **Host nginx changed with it, ≈18:47Z.** The `bestshiny.com` server block — the one the
  browser's `/api/` calls traverse — had no `proxy_read_timeout`, so nginx's 60 s default would
  have cut a slow director turn before the container's new 300 s could matter; the
  `api.bestshiny.com` block already carried 300 s. `/etc/nginx/sites-available/bestshiny` now sets
  `proxy_read_timeout 300s` in that location too (`.bak-20260906-184713` holds the previous file;
  `nginx -t` clean, graceful reload, site 200). The chain is host 300 s → web container 300 s →
  api 120 s model timeout.

  **Rollback.** No migration, so a code rollback to `5010ce5` is a plain redeploy of that
  revision; the pre-extraction `bestshiny-backup` dump and the `*.bak-20260906-184534` copies of
  `.env` and the compose file remain available. Direct uploads authorized under the old
  content-addressed keys inside their one-hour window replay without a write credential
  (§2.49 residuals).

- **Previous release.** `5010ce5` (`main`, [#58](https://github.com/Ethanwrite/bestshiny/pull/58)
  — web-only: `apps/web/nginx.conf` gains `client_max_body_size 100m`), deployed 2026-09-06 ≈13:24Z.
  `DEPLOYED_SHA.prev = fe91a8f`. No migration, no `.env` change; api and worker were not touched
  (same image IDs as the `fe91a8f` release), only `web` was rebuilt and force-recreated
  (`deploy_web_remote.sh <sha>`, log `deploy-web-5010ce5.log`, `DEPLOY_EXIT=0`).

  **The defect.** Every reference image over 1 MB failed on the site with HTTP 413. The 413 came
  from the **web container's nginx**: `apps/web/nginx.conf` never set `client_max_body_size`, so
  nginx's 1 MB default answered `POST /api/v1/assets` with its own 585-byte HTML 413 — visible in
  the web container's access log and absent from the api's. Neither of the two layers that were
  meant to decide the limit was reached: the host nginx allows `128m`, and the api's request fence
  answers a JSON 413 at `MAX_UPLOAD_BYTES` (100 MB). The proxy now declares the api's limit;
  `tests/test_reference_upload_contract.py` pins it to `Settings.max_upload_bytes`. The old UI had
  hidden this behind a generic "Reference upload failed", which is why it surfaced only after the
  `fe91a8f` release started reporting the real status.

  Verified after: markers, `web` running image equals the built one, restarts 0, public
  `api.bestshiny.com/health` and `bestshiny.com/app` 200, the new bundle `/assets/index-QiN20SXU.js`
  names a bodiless 413, `nginx -T` inside the container shows `client_max_body_size 100m`, and the
  proof: a 5 MB multipart `POST /api/v1/assets` through the public web proxy without auth now
  answers the api's `401 {"detail":"请先登录"}` with the whole body uploaded, where before the fix
  nginx answered 413 without forwarding it. Zero tracebacks. **When a symptom is "413 on upload",
  read the web container's access log first: three nginx layers can answer 413 and only the api's
  is JSON.**

- **Previous release.** `fe91a8f` (`main`, [#55](https://github.com/Ethanwrite/bestshiny/pull/55)
  — reference images reach Seedream, one dominant action per shot, execution duration is the
  router's, identity references narrow to identity-critical characters, both multimodal
  embeddings on as advice; ledger entry `docs/OPEN_ISSUES.md` §2.48), deployed 2026-09-06 ≈05:30Z.
  `DEPLOYED_SHA.prev = 4ab15cb`. One migration ran, `0080` → **`0081_veo_discrete_durations`**:
  data only, no DDL — `supported_durations = [4, 6, 8]` written onto the three OpenRouter Veo
  capability profiles (verified on all three rows afterwards) and the old "gap" note on the
  `veo-3.1-openrouter` definition swapped for the current note. No `.env` change was made: the two
  flags this release adds or flips carry code defaults.

  Same ordering as the `4ab15cb` deploy (`deploy_remote.sh`, log `deploy-fe91a8f.log`,
  `DEPLOY_EXIT=0`, ~4 min including a full pip layer rebuild): backups → extract
  (`COMPOSE_UNCHANGED`) → build → `stop worker` → explicit `alembic upgrade head` on the new image →
  `up -d api web` → `up -d --force-recreate --no-deps web` → `/health` (answered on the first
  check) → `up -d worker`.

  Verified after: both markers written, `alembic current` = `0081`, all three running image IDs
  equal the freshly built ones, `RestartCount=0` on all three, local 8080/3000 200, public
  `api.bestshiny.com/health` and `bestshiny.com/app` 200 from the host, the new bundle
  `/assets/index-Bc-S0XTz.js` carries the reference-preview and upload-refusal strings, the api's
  startup sync inserted the `MULTIMODAL_EMBEDDING` FALLBACK binding (`gemini-embedding-2-openrouter`,
  priority 10) beside the Voyage PRIMARY, zero tracebacks in api or worker, and data untouched
  (14 sessions, 28 jobs, 6 anchors, 0 style locks, `memory_index_outbox` empty).

  **The two host `.env` overrides were flipped on the operator's confirmation, ≈05:50Z the same
  day.** The host carried `FEATURE_VOYAGE_MEMORY=false` and `FEATURE_SEMANTIC_STYLE_LOCK=true`, both
  of which beat the new code defaults. They now read `FEATURE_VOYAGE_MEMORY=true` and
  `FEATURE_SEMANTIC_STYLE_LOCK=false` (`.env.bak-<stamp>` holds the previous file; the diff is
  exactly those two lines). `api` and `worker` were force-recreated one after the other (api healthy
  after 5 checks, ~15 s of 502; worker after it), on the same image IDs, restarts 0, zero
  tracebacks; both containers report `FEATURE_VOYAGE_MEMORY=true`, `FEATURE_SEMANTIC_STYLE_LOCK=false`
  and `PROVIDER_MODE=live`, so the semantic style layer now runs in **advisory** mode and the memory
  outbox drains. Enabling memory billed no backlog (the outbox was empty), and no style lock existed,
  so switching the layer to advisory affected no existing gate.

  **Rollback caveat.** A code rollback to `4ab15cb` requires a downgrade to `0080` (that image pins
  `REQUIRED_SCHEMA_REVISION = 0080_creative_turn_claims`); `0081`'s `downgrade()` removes the
  `supported_durations` key and restores the gap note, and is exercised by
  `tests/test_execution_duration.py`, never on this populated database. The pre-extraction
  `bestshiny-backup` dump and the `*.bak-<stamp>` copies of `.env` and the compose file remain the
  safer path.

- **Earlier release.** `4ab15cb` (`main`, [#52](https://github.com/Ethanwrite/bestshiny/pull/52)
  the creative-director audit's P1/P2 findings, on top of
  [#51](https://github.com/Ethanwrite/bestshiny/pull/51) the audit's medium/low findings and
  [#50](https://github.com/Ethanwrite/bestshiny/pull/50) the `75ea271` release record), deployed
  2026-09-05 ≈04:47Z. `DEPLOYED_SHA.prev = 75ea271`. One migration ran, `0079` →
  **`0080_creative_turn_claims`**: a single `CREATE TABLE` (unique `(session_id, client_turn_id)`,
  FK to `creative_sessions`, one index), no data migration. No `.env` change: #51's
  `memory_index_retention_days` and everything #52 adds carry defaults. The six migrations
  `0073`–`0078` are *modified* in this delta (#51 made them skip-if-present) and were already
  applied here, so alembic did not re-run them.

  **The DDL ordering from the `d491870` deploy was used again, plus an explicit upgrade.** The
  host script (`deploy_remote.sh`, log `deploy-4ab15cb.log`, `DEPLOY_EXIT=0`, ~2 min) ran
  backups → extract → build → `stop worker` →
  `docker compose run -T --rm --no-deps api alembic upgrade head` on the *new* image →
  `up -d api web` → `up -d --force-recreate --no-deps web` → wait for `/health` → `up -d worker`.
  The api container's own startup upgrade was then a no-op, which is the point: the migration
  ran once, deliberately, with no worker on the old image and before the old api was replaced.

  Verified after: markers both written, `alembic current` = `0080`, all three running image IDs
  equal the freshly built ones (`up -d` skipped nothing this time; `web` was force-recreated
  anyway), `RestartCount=0` on all three, api healthy, local 8080/3000 200, public
  `api.bestshiny.com/health` and `bestshiny.com` 200 from the host, `COMPOSE_UNCHANGED`, the
  OpenAPI document carries `accept_inherited_style`, the new bundle `/assets/index-DOOyYWG0.js`
  carries the style-conflict notice and its checkbox, `creative_turn_claims` exists and is empty,
  `memory_index_outbox` empty, zero tracebacks in api or worker, and data untouched (14 sessions,
  28 jobs, 6 anchors — the same counts as the previous release).

  **What operators will see that they did not before.** A duplicate `client_turn_id` sent while
  the first request is still being answered gets `409 TURN_IN_PROGRESS` (retryable) instead of a
  second paid director call. A bible whose brief asks for a look other than the project's locked
  style is refused with `STYLE_LOCK_CONFLICT` until the approval carries `accept_inherited_style`
  (the web page shows both looks and a checkbox). A USER_STATED claim whose quote does not say the
  value is demoted (`VALUE_NOT_IN_EVIDENCE`), so a few more brief values may surface as assumptions
  to confirm. A Modal callback that reports only some characters leaves the submission ACCEPTED
  under its deadline rather than REPORTED. Prohibitions now reach every shot's prompt, negative
  prompt and QC checklist.

  **Rollback caveat.** A code rollback to `75ea271` requires a downgrade to `0079` (that image pins
  `REQUIRED_SCHEMA_REVISION = 0079_voyage_video_pixel_price`); `0080`'s `downgrade()` only drops the
  new table and has been exercised in the migration round-trip test, never on this populated
  database. The pre-extraction `bestshiny-backup` dump and the `*.bak-20260905-044537` copies of
  `.env` and the compose file are the safer path.

- **Data correction, 2026-09-05 ≈05:03Z — one recovery message rewritten in place.**
  Not a release: no deploy, no migration, no restart. One `UPDATE` against
  `creative_turns`, at the operator's request.

  Migration `0075` wrote a DIRECTOR turn on the single session it recovered
  (`743d34cd-8ab2-4b07-bda8-79bf3cee44f0`, turn `8f4668c5-50c7-4b86-8681-f42f274cc0aa`,
  sequence 9) telling the user *"key visuals whose look has not changed are re-used rather
  than generated again"*. That is not what happens: the way back out of the recovered stage
  runs a live DIRECTOR call that writes a **new** screenplay, anchor prompts derive from it,
  and the hash matches only when the model reproduces the old phrasing. The sentence was a
  promise about money the platform could not keep.
  [#51](https://github.com/Ethanwrite/bestshiny/pull/51) corrected the constant — but a
  constant only governs future runs, and alembic will not re-run an applied migration, so the
  row `0075` had already written on this host still carried the original text.

  The replacement was parsed out of `RECOVERY_MESSAGE` in the migration's own AST rather than
  retyped, so the row now says exactly what today's code would write. **The original is kept**
  in `context_json` beside `message_corrected_at` and `message_corrected_reason`: this is
  machine-authored dialogue, and correcting it must not look like it never said anything else.

  Procedure worth repeating for any hand-written production `UPDATE`: `bestshiny-backup` first
  (`video_platform-20260905T050255Z.dump`), then a statement triple-guarded on the row id, on
  `reasoner = 'MIGRATION'` and on `context_json->>'migration'`, inside `BEGIN`/`COMMIT` with
  `ON_ERROR_STOP`. `UPDATE 1`. The first attempt failed on `operator does not exist: json ||
  jsonb` — **`context_json` is a `json` column, not `jsonb`**, so a merge needs a
  `::jsonb … ::json` round trip; the error aborted the transaction and wrote nothing, which is
  what the explicit `BEGIN` is for. Verified after: content matches the constant, the original
  is preserved, still exactly one `MIGRATION` turn, the session still `BRIEF_APPROVED`,
  `/health` 200.

- **Previous release.** `75ea271` (`main`, [#48](https://github.com/Ethanwrite/bestshiny/pull/48)
  the four defects a pre-deploy audit found in the shipped tree, and
  [#49](https://github.com/Ethanwrite/bestshiny/pull/49) the voyage video-pixel price), deployed
  2026-09-04 ≈23:05Z. `DEPLOYED_SHA.prev = d491870`. One migration ran,
  `0078` → **`0079_voyage_video_pixel_price`**: a single guarded INSERT, no DDL. No `.env` change.

  `0075` is *modified* in this delta but was already applied here, so alembic did not re-run it —
  its scoping fix reaches fresh databases only, which is what its commit says. Read the delta as
  `git diff --name-status d491870 75ea271`: modified migrations in a delta are not re-applied
  migrations, and confusing the two is how a release gets credited with a repair it did not make.

  **`up -d` skipped `web` again — the fourth time on record.** It reported success while `web` sat
  at `Up 9 hours` on the previous image (`14907147…` against a freshly built `c361392c…`).
  `up -d --force-recreate --no-deps web` fixed it. The web bundle was byte-identical either way
  (nothing under `apps/web/` changed in this delta), so nothing user-visible was stale — but the
  container was running an image built from the *previous* commit while `DEPLOYED_SHA` claimed this
  one. **Compare every container's running image ID against the built one, every deploy.**

  Verified after: markers both written, `alembic current` = `0079`, all three running image IDs
  equal the built ones, api healthy, local 8080/3000 200, public `api.bestshiny.com/health` and
  `bestshiny.com` 200, `COMPOSE_UNCHANGED`, zero tracebacks in api or worker, `memory_index_outbox`
  empty, and data untouched (14 sessions, 28 jobs, 6 anchors — the same counts as before). The three
  official voyage-multimodal-3.5 list prices now all exist:

  | input_mode | billing_unit | unit_price | effective_from |
  | --- | --- | ---: | --- |
  | `input_tokens` | `1M_tokens` | 0.12 USD | 2026-09-02 (0071) |
  | `image_input` | `1B_pixels` | 0.60 USD | 2026-09-02 (0071) |
  | `video_input` | `1B_pixels` | 0.60 USD | 2026-09-04 (0079) |

  Video is the image rate because the vendor counts each frame as an image, which is also how this
  platform sends video: as extracted stills. `settle_from_usage` no longer returns UNCERTAIN for a
  usage block reporting video pixels.

- **Previous release.** `d491870` (`main`, [#46](https://github.com/Ethanwrite/bestshiny/pull/46)
  the creative-director production chain: director intent reaches generation, Scene/Product/Prop
  become real Canon, brief↔screenplay conformance, server-verified USER_STATED, optimistic
  concurrency on dialogue and screenplay, a resumable visual-bible lock, legacy-session recovery,
  full character identity coverage, key-visual retry, browser turn-id idempotency, Voyage frames
  and pixel settlement, an advisory memory outbox, and multi-character Modal shadow analysis),
  deployed 2026-09-04 ≈13:40Z. `DEPLOYED_SHA.prev = be6e10d`. Six migrations applied by the api
  container's own `alembic upgrade head`, `0072_creation_soft_delete` → **`0078_creative_session_create_idempotency`**:
  `0073` shot intent, `0074` lock steps, `0075` legacy session recovery (data), `0076` character
  evidence coverage, `0077` memory index outbox, `0078` session create idempotency. No `.env`
  change and no new required environment variable; the two new settings
  (`memory_index_sweep_interval_seconds`, `memory_index_sweep_limit`) carry defaults.

  **The worker was stopped before extraction and started only after the api reported head.**
  That is a deliberate departure from the blanket `up -d` in §4: `0073` takes an ACCESS EXCLUSIVE
  lock on `shots`, all six migrations run in one transaction, and the new candidate-commit path
  enqueues into `memory_index_outbox` — a table that does not exist until `0077`. A worker left
  running on the old image can block the DDL or commit into a missing table. Prefer
  `stop worker` → `up -d api web` → wait for head → `up -d worker` whenever a release carries DDL.

  `0075` acted on exactly one session on this host: `743d34cd-8ab2-4b07-bda8-79bf3cee44f0`,
  stranded at BIBLE_PROPOSED with `current_screenplay_revision = 0`, no `creative_screenplays`
  row and no compiled episode, moved to BRIEF_APPROVED with one appended MIGRATION turn. Its
  approved brief was confirmed to exist *before* the deploy. All 6 `creative_visual_anchors`
  had their empty `prompt_hash` backfilled — note that the backfill is **unscoped**, so 3 of
  those 6 belong to the COMPILED session `49c9e983`, which the migration's own docstring says
  it leaves alone. On a compiled session the hash has no behavioural effect, but the docstring
  overstates the scope and the backfill is not restored by `downgrade`.

  Verified after: `alembic current` = `0078` (head), api healthy, `127.0.0.1:8080/health` 200 and
  `:3000` 200, public `api.bestshiny.com/health` 200 and `bestshiny.com` 200, all three running
  image IDs equal the built ones, `docker-compose.prod.yml` byte-identical after extraction
  (`COMPOSE_UNCHANGED`), the three new tables present, `memory_index_outbox` empty, and zero
  tracebacks in api or worker. `voyage_memory` is **off** (no `feature_flags` rows exist at all),
  so the 120-second outbox sweep defers every row without an embedding call; enabling it later
  drains whatever backlog has accumulated, so check
  `SELECT count(*) FROM memory_index_outbox WHERE status='PENDING'` first and enable per project.

  **Rollback caveat, recorded because nothing downgrades it.** `0073`–`0078` have never had their
  `downgrade()` bodies run against a populated database, and a code rollback to `be6e10d` *requires*
  a downgrade to `0072` because the old image pins `REQUIRED_SCHEMA_REVISION = 0072_creation_soft_delete`.
  Separately, any screenplay written after this deploy stores `invariants` / `required_copy` as
  objects, which the pre-`d491870` schema cannot parse; rolling back needs those rows rewritten to
  arrays of their `text` values (`SELECT id, session_id FROM creative_screenplays WHERE created_at > '<deploy time>'`)
  or they are unreadable. Restore from `backups/` is the safer path, and it is still unrehearsed.

- **Previous release.** `be6e10d` (`main`, [#44](https://github.com/Ethanwrite/bestshiny/pull/44)
  delete a creation without rewriting what it cost, on top of
  [#45](https://github.com/Ethanwrite/bestshiny/pull/45) the role runtime's capability catalogue),
  deployed 2026-09-03 ≈22:54Z, Alembic `0072_creation_soft_delete`. This line was written
  retrospectively on 2026-09-04 from `DEPLOYED_SHA.prev`: the release itself was never recorded
  here, which is the same drift §6 warns about two entries down.

- **Previous release.** `aa8d5d3` (branch `claude/bestshiny-director-workflow-1a6b59`), deployed
  2026-09-03 with the §4 backup/build/recreate procedure. Migration
  `0071_voyage_official_provider` moved `MULTIMODAL_EMBEDDING` from the retired
  `openrouter / voyageai/voyage-multimodal-3.5` definition to
  `voyage / voyage-multimodal-3.5`, registered the official
  `/v1/multimodalembeddings` adapter and official token/pixel list prices. The production
  Voyage credential was replaced without entering the repository. A real text+PNG call through
  `ModelRoleRuntime` succeeded: execution `cc9782d5-6b10-4a97-af63-59bf6947f66f`, 512-dimensional
  `EmbeddingEvidence` `d5db874d-b31a-4333-a9f7-11da531de5fc`; provider usage reported 18 text
  tokens and 50,176 image pixels. API/worker/web and PostgreSQL were healthy after the deploy.

- **Previous release.** `c3b184b` (branch `claude/bestshiny-director-workflow-1a6b59`, the
  director-writes overhaul; PR against `main` opened right after the deploy), deployed from the
  session on 2026-09-03 ≈01:08Z with the §4 procedure run as a `nohup` script on the host (log
  `/opt/bestshiny/deploy-c3b184b….log`), pre-extraction backups of `.env` and the compose file
  (`*.bak-20260903-010808`) plus `bestshiny-backup`, `DEPLOYED_SHA.prev = 86987c3`, web
  force-recreated. **Migration `0070_creative_director_screenplay`** applied by the api container's
  own `alembic upgrade head`; no env change. Verified: api healthy, `/health` 200, web 200, 19
  creative routes (screenplay + lineage present), all three running image IDs equal the built
  ones, compose file unchanged, zero tracebacks, budget windows at 200 USD/day. Note: production
  runs a branch commit until the PR merges — once it does, redeploy from `main` (or record the
  squash SHA) so `DEPLOYED_SHA` names a commit that survives branch cleanup.

- **Previous release (2026-09-02).** `86987c3` (`main`, [#41](https://github.com/Ethanwrite/bestshiny/pull/41)
  the director answers in its own words, images stop carrying video parameters, conversations
  and never-generated shots can be deleted), deployed from the session on 2026-09-02 ≈21:25Z
  with the §4 procedure plus the pre-extraction backups and `DEPLOYED_SHA.prev = cb316c2`; no
  env change, no migration (still `0069_production_budget`). Verified: api healthy, `/health`
  200, web 200, all three running image IDs equal the built ones, compose file unchanged,
  budget policy enabled, zero tracebacks. The dev stack was rebuilt from the same tree.

- **Earlier the same day.** `cb316c2` ([#40](https://github.com/Ethanwrite/bestshiny/pull/40)
  OpenRouter images send only the parameters the model declares), deployed ≈20:55Z, `.prev =
  34c9323`, no env change, no migration. The failed gpt-image-2 job `da3b1a8e` from before it
  was refunded (1 credit) and its spend authorization released, both audited.

- **Previous release.** `34c9323` (`main`, [#39](https://github.com/Ethanwrite/bestshiny/pull/39)
  the automatic production budget: credits are the user's gate, no canary permit on paying
  traffic), deployed by the operator on 2026-09-02 ≈19:45Z with the §4 procedure plus a
  pre-extraction backup of `.env` and the compose file, `DEPLOYED_SHA.prev = 0f90f0b`, Alembic
  `0069_production_budget` (applied by the api container's own `alembic upgrade head`), and
  one appended `.env` line `PRODUCTION_BUDGET_PLATFORM_USD_PER_DAY=200` (platform breaker;
  no per-provider ceiling). Verified after: api healthy, `GET /health` 200, api/worker/web
  running image IDs equal the built ones, `GET /internal/production-budget` `enabled: true`
  with the platform row at 200. Then `ALLOW_RUNAPI_EDGE_CALLS=true` was set and api + worker
  recreated (the low-cost prompt refiner's RunAPI path had been refused by its own gate; the
  `runapi` provider budget row is 10 USD, unused).

- **Previous release.** `0f90f0b` (`main`, [#38](https://github.com/Ethanwrite/bestshiny/pull/38)
  XunHuPay checkout on top of [#37](https://github.com/Ethanwrite/bestshiny/pull/37) cold-start
  routing admission and [#36](https://github.com/Ethanwrite/bestshiny/pull/36) scene-champion
  routing), deployed 2026-09-02 14:29Z, Alembic `0068_xunhupay`. `DEPLOYED_SHA.prev` is
  `9109186`. Verified post-deploy: api and worker on the rebuilt image IDs, `web` recreated
  by hand after `up -d` skipped it (third time on record — check every container's running
  image ID against the built one, every deploy), api healthy, public `/health` and homepage
  200, zero tracebacks. `ROUTER_ADMISSION_POLICY` is not set on the host, so the code
  default `cold_start` applies, and `POST /internal/router/video` answers `200` with
  `video-router-v3` / `CHAMPION_TABLE` for the first time in production (plain shot →
  Seedance, start+end frame → Wan 2.7). Set `ROUTER_ADMISSION_POLICY=strict` in `.env` and
  recreate api + worker when the catalogue has earned its lifecycle states.

- **Next release carries migration `0069_production_budget`** (two new tables, no data
  movement, refuses to downgrade over recorded authorizations) and the automatic production
  budget, which is **off until `.env` names a ceiling**:

  ```
  PRODUCTION_BUDGET_PLATFORM_USD_PER_DAY=50
  PRODUCTION_BUDGET_PROVIDER_USD_PER_DAY=seedance=30,openrouter=30,wan=20
  ```

  With those unset the deployed behaviour is unchanged: every live call needs a
  `LiveCanaryPermit` — which is what kept production unusable behind expired permits until
  2026-09-02. With them set, **every enabled, live-enabled, priced model** (21 of the 24 in the
  production catalogue; the three Flow/Wan-3.0-official rows have no key or no verified price)
  runs user traffic on credits plus a quote-bound spend authorization under the daily breaker,
  and consumes no permit. The ceiling is the platform's own protection against a wrong price,
  a free credit grant or a burst — set it well above what users can actually buy through
  credits, or it becomes the very block it replaced. Verify after the deploy with
  `GET /internal/production-budget` (policy `enabled`, today's rows) — see
  `docs/OPEN_ISSUES.md` §1.18 for what each field means and what a 503 at job creation is.

  **How that day went, because it will recur.** `2090992` (#36) was deployed at 09:48Z with
  `.prev = 89e1126` (the `codex/sponsored-usdc-walletconnect` tip, tree-identical to #35's
  squash `48f62d6`, proven with an empty `git diff` before extraction). About 47 minutes
  later another session deployed the **unmerged** `codex/xunhupay-production` tip
  `9109186` directly, migrated production to `0068`, and rewrote `.prev` back to `89e1126`.
  That left `main` undeployable — its head migration was `0067`, the api refuses an unknown
  revision, and shipping it would have removed the payment feature — which was caught only by
  `git diff --name-status <DEPLOYED_SHA> <candidate>` before extraction. Resolution: open a
  PR for the branch (#38), gate the merged tree on both engines (which surfaced a
  PostgreSQL-only `create_all` idempotency bug on `main` since #35, fixed in #38), merge,
  then deploy `main`. **Run that diff before every extraction, and never deploy a commit
  that is not on `main` without recording why.**
  Earlier: `8b92639` (#23, 2026-08-30, FREE gates + rebrand); `7e80d5a` #22 and `f758a9c`
  were deployed between #19 and #23 without this line moving — `DEPLOYED_SHA` was right
  throughout, this document was behind, which is exactly the failure mode the paragraph
  below describes.

  **Read this before trusting any "in sync" claim.** The previous handover recorded
  production as `9eb2934`; it was actually on `4832066`, one release behind. #18 was
  merged and never deployed. It was documentation-only, so no code was stale and nothing
  misbehaved — which is exactly why it went unnoticed for a day. What caught it was
  hashing files on the host against candidate commits, not reading a document. Treat a
  written release claim as a hypothesis and check `DEPLOYED_SHA` (or, if it is missing
  because the deploy predates it, `alembic current` plus the presence of files a known
  commit added).

- **Payment configuration.** Server-side payment configuration was completed on
  2026-08-30. The production `.env` now carries the DePay callback public key,
  Alchemy webhook signing key and Base treasury address; the file and its rollback
  copy remain mode `0600`. The API and worker were recreated without rebuilding
  images. Runtime validation reports both the fixed DePay checkout and signed
  callback as configured for the 30 USDC → 3,000 Credits → permanent PRO offer.
  Public health returns `200`, while unsigned DePay and Alchemy callback probes are
  rejected with `401`, proving that both authentication boundaries are active.
  `ALCHEMY_CREDITING_ENABLED` and `LEGACY_WALLET_PAYMENTS_ENABLED` remain `false`:
  DePay is the canonical credit issuer and Alchemy remains independent chain/reorg
  evidence. This closes server configuration only; a deliberately authorized real
  payment and DePay-dashboard parameter review remain separate live evidence.
- **Backups.** `/usr/local/bin/bestshiny-backup` takes a nightly custom-format `pg_dump`
  into `/opt/bestshiny/backups` at 03:15 UTC and keeps 14 days. Restore is
  `pg_restore -U video_platform -d video_platform --clean`. **Restore has not been
  rehearsed on this host** — the readiness checklist item for backup/restore drills is
  still unchecked and this does not close it.
- **Log growth.** `/etc/docker/daemon.json` caps container logs at 20 MB × 5 files. The
  worker polls continuously and would otherwise fill the disk on its own.
- **Restart policy.** Every service is `restart: unless-stopped`, and docker and nginx
  are enabled units, so the stack returns after a reboot.
- **Reaching the host from a developer laptop.** Both SSH and HTTPS need help here. SSH
  needs `ssh -B en0` (`scp` needs `-o BindInterface=en0`, since its own `-B` means batch
  mode) because the local TUN proxy makes every TCP port appear open and then kills 22.
  `curl` to `api.bestshiny.com` needs the *opposite* treatment: the hostname resolves
  into the proxy's `198.18.0.0/15` fake-IP range, so `--interface en0` binds to an
  address that goes nowhere and times out. Either let curl use the proxy normally, or
  bypass DNS entirely:

  ```bash
  curl -sS -k --interface en0 --noproxy '*' -H 'Host: api.bestshiny.com' https://153.75.95.10/health
  ```

  A timeout from one of these is not evidence the host is down. Check the other path
  before concluding anything.

## 7. The first administrator

`platform_role` starts at `USER` for everyone, and every role change goes through
SUPER_ADMIN RBAC — which leaves the first one unreachable. `POST
/internal/admin/bootstrap-super-admin/{user_id}` is the way in: it takes the platform API
key rather than a session, refuses with `409` once any SUPER_ADMIN exists, and writes a
`SUPER_ADMIN_BOOTSTRAPPED` row to the admin audit log. Register through the site first —
nothing here creates an account.

```bash
cd /opt/bestshiny
K=$(grep -E '^PLATFORM_API_KEY=' .env | cut -d= -f2-)
UID=$(docker compose -f docker-compose.prod.yml exec -T postgres psql -U video_platform \
  -d video_platform -tAc "select id from users where email = 'you@example.com'" | tr -d '[:space:]')
curl -sS -XPOST -H "Authorization: Bearer $K" \
  "http://127.0.0.1:8080/internal/admin/bootstrap-super-admin/$UID"
```

Credits are then a SUPER_ADMIN action through the Console's own API, not a database edit,
so the grant lands in `admin_credit_adjustments` with a reason and shows up in the audit
trail like any other. `delta` is capped at 1,000,000 per adjustment and `Idempotency-Key`
is required — replaying the same key returns the original adjustment rather than granting
twice.

```bash
curl -sS -XPOST -H "Authorization: Bearer <your session token>" \
  -H "Idempotency-Key: topup-$(date -u +%Y%m%d)-1" -H "Content-Type: application/json" \
  -d '{"workspace_id":"<workspace>","delta":1000000,"reason":"Test budget for the full flow"}' \
  https://api.bestshiny.com/api/admin/users/<user_id>/credit-adjustments
```

A session token comes from signing in, or from `scripts/canary_session.py --email <address>`,
which mints a short-lived one through the application's own `AuthService` so nobody has to
paste a password. The starter grant is 50 credits and a single 4-second video reserves about
44, so the ten live-canary targets need a top-up before the sweep will run.

## 8. What this deployment resolved, and what it did not

Three blockers in the handover documents were environmental, and moving off the
developer laptop cleared them:

- **`RC_HANDOFF_2026-08-29.md` §3.1** — every provider hostname resolved into a
  fake-IP proxy range on the development machine, so artifacts could never be
  downloaded and the SSRF fence in `media_service/registry.py` refused them, correctly.
  On this host `openrouter.ai`, `dashscope.aliyuncs.com`, `ark.cn-beijing.volces.com`
  and the rest all resolve to real global addresses.
- **`OPEN_ISSUES.md` §1.3** — object storage now exists *and* answers the browser
  preflight, so reference media, image edits and direct uploads are no longer failing
  closed.
- **Character Evidence** was `BLOCKED_EXTERNAL` on `api.bestshiny.com` being publicly
  HTTPS-reachable. It now is. `CHARACTER_EVIDENCE_ENABLED` is still `false`; the Modal
  half has never been deployed, and turning the flag on before it is would fail startup
  closed, by design.

What it did not resolve, because none of it is environmental:

- **No accounts exist.** The database has zero users, workspaces and projects. The live
  canary needs a real session token and a project id and
  [deliberately creates neither](../scripts/live_canary.py); `scripts/canary_session.py`
  will mint a short-lived session for a user that already exists, but a real address is
  never auto-selected.
- **Payment webhooks are unconfigured.** `ALCHEMY_*` and `DEPAY_CALLBACK_PUBLIC_KEY` are
  empty, so on-chain crediting cannot run end to end. These are operator secrets.
- **`wan-3.0-official`** stays disabled pending a DashScope invitation. The other 23
  models are `live_enabled` after `POST /internal/models/reconcile-live?apply=true`.
- The unchecked P0 items in `PRODUCTION_READINESS_CHECKLIST.md` are unchanged by
  deploying. A running site is not the same claim as a released one.
