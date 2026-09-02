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

- **Current release.** `0f90f0b` (`main`, [#38](https://github.com/Ethanwrite/bestshiny/pull/38)
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
