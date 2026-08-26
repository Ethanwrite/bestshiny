# Integration QA log — merged baseline (Studio rebuild + Admin Console)

Baseline: working tree on top of `60fe9ca`, schema `0042_admin_console`.
Stack rebuilt 2026-08-25: compose `api` / `worker` / `web`, PostgreSQL 17, Alibaba OSS HK.

Severity: **S1** blocks a core flow · **S2** wrong behaviour with a workaround ·
**S3** visual/polish · **S4** note, no action needed.

---

## 1. Gates — all green

| Gate | Result |
| --- | --- |
| `ruff check .` | All checks passed |
| `mypy` | Success, 139 source files |
| `alembic heads` | `0042_admin_console`, single head |
| `pytest -q` (SQLite) | **716 passed**, 9 skipped |
| `pytest -q --database=postgres` | **718 passed**, 7 skipped |

Migration `0042` was rehearsed on a `pg_dump` clone of the dev database (upgrade
and downgrade both clean, 4 users / 4 projects / 25 models preserved) before being
applied for real. Pre-migration dump kept at
`scratchpad/video_platform_pre0042.sql`.

## 2. Coverage

Public site (`/`, `/product`, `/models`, `/pricing`), auth (`/login`, `/signup`),
Studio (Create / Director / Productions), Admin Console (Overview, Users, Credits,
Models, Providers, Routing, Jobs, Projects, System Health, Audit Logs).
Roles: workspace user (`ui-rebuild-20260825@example.com`) and platform
`SUPER_ADMIN` (`qa-admin-20260825@example.com`, promoted through
`/internal/admin/bootstrap-super-admin`, audit row written).
Viewports 1440 × 900 and 1920 × 1080.

Checked and clean: routing + hard refresh on every path (nginx SPA fallback returns
200 for all), shell isolation (exactly one of public/app/admin mounted, verified
per route), **CSS leaks** (`admin.css` and `public.css` are 100 % scoped — every
selector is under `.admin*` / `.pub*` / `:root`), horizontal overflow (0 px at both
widths on every route), drawers and modals, tables, console and network errors.

---

## 3. Fixed in this pass

### QA-001 — S1 — Object storage rejected every upload
`PutObject` failed for all media with
`NotImplemented: Aws MultiChunkedEncoding STREAMING-UNSIGNED-PAYLOAD-TRAILER is not supported`.
botocore ≥ 1.36 adds a CRC32 trailer to every upload by default; Alibaba OSS does
not implement that encoding. The bucket held **0 objects** — nothing had ever
reached the durable plane.

Fix: `request_checksum_calculation="when_required"` on the boto3 `Config` in
`packages/shared/platform_shared/storage.py`. Explicit checksums (the presigned
path's `enforce_checksum`) are still sent, so the integrity guarantee is unchanged.
Verified by `scripts/verify_object_storage.py` (`client transfer HTTP 200`) and by a
real reference-image upload through the Create → Project assets dialog.

### QA-003 — S1 — Admin Console was entirely non-functional behind nginx
Every admin request 404'd. `admin.js` used `const API = ""` while nginx
`proxy_pass http://api:8080/` strips the `/api/` prefix, so `${API}/api/admin/...`
arrived as `/admin/...`. `app.js` already carries `/api` in its base for this reason.

Fix: `admin.js` now uses the same base as `app.js`. `/api/api/admin/dashboard → 200`,
console renders live data.

### QA-004 — S2 — Toast was invisible over any modal
A modal `<dialog>` renders in the top layer, which no `z-index` can reach, so
failure toasts raised during a dialog flow were never seen — including "Image upload
failed" during QA-001. Fix: the toast is now `popover="manual"` and shown via
`showPopover()`, putting it in the top layer too. Verified visible over an open dialog.

### QA-005 — S2 — Asset dialog gave no error feedback
On upload failure the dialog's status line kept its default helper text, reading as
"nothing happened". Fix: failures now render inline in `#manualAssetStatus` with an
`is-error` treatment, in addition to the toast.

### QA-006 — S3 — Raw epoch in a user-visible version label
Version history showed `User upload v1787699575206`. Now a localised timestamp.

### QA-007 — S3 — `<img>` with empty `src`
`#depayQrCode` shipped with no `src`, which makes browsers refetch the document.
Given a transparent placeholder until a checkout mints a real code.

### QA-008 — S2 — Create defaulted to a provider that cannot run
The default model was `Google Flow · NARWHAL`, and `google_flow` is the one provider
whose transport probe fails (see QA-009) — so the primary demo path was a guaranteed
failure. Resolved **through the product**, not in code: `google_flow` disabled from
Admin → Providers with a recorded reason. Create now defaults to
`openrouter · openai/gpt-image-2` (15 CR). Re-enable from the same screen.

Also aligned `app.js` with the endpoint's new contract: `/v1/providers` now filters
server-side and no longer returns `configured` / `healthy`, so the client no longer
re-filters on fields that are absent.

---

## 4. Open — needs a decision, not touched

### QA-009 — resolved 2026-08-25 — see §11.

### QA-010 — S3 — Danger density on Admin → Providers
Nine solid-red `Disable` buttons render at once, one per provider card, including on
providers that are merely not configured. Against the brief's own rule (red reserved
for real failure/critical), a tertiary or outline treatment until hover would read
better. Cosmetic, inside Codex's shell, so not changed.

### QA-011 — S3 — Model Registry table scrolls past its useful columns
The capability list forces 1937 px; the box is 1118 px at 1440 and 1558 px at 1920,
so `Pricing` and `Last live test` are always off-screen. It scrolls correctly inside
its own container and the page never overflows, so this is a density choice rather
than a bug — truncating the capability list would surface the operational columns.

### QA-012 — S4 — `Reconcile` visible to workspace owners
Productions shows `Reconcile` to any workspace OWNER/ADMIN. The endpoint authorises
project writers, so this is not a privilege leak, but it is an operator-shaped verb
in a user-facing panel.

### QA-013 — fix written, one command from resolved — see §11.

### QA-014 — S4 — OSS does not enforce `x-amz-checksum-sha256`
`[WARN] checksum enforcement — store accepted mismatched bytes`. Already documented
in `storage.py`; Content-MD5 fallback proves transit integrity but not that the bytes
match the key's SHA-256. Unchanged by this pass.

### QA-002 — S4 — Do not source `.env` into the PostgreSQL gate
`set -a; source .env` exports real OSS credentials into pytest, which then writes to
the production bucket and fails 151 tests via QA-001. Export only `POSTGRES_PASSWORD`,
as `HANDOFF.md` says. Recorded because the failure looks like a code regression.

---

## 5. Test data created

| Item | Value |
| --- | --- |
| Workspace user | `ui-rebuild-20260825@example.com` / workspace "Rebuild QA" |
| Platform admin | `qa-admin-20260825@example.com` / `SUPER_ADMIN` |
| Project | "Vertical short drama" — 1 episode, 2 scenes, 3 shots, 3 characters |
| Asset | "QA upload probe" (REFERENCE, v1, canonical) |
| Provider control | `google_flow` disabled with reason (QA-008) |

---

## 6. Gate state at hand-off — red, and not from this pass

After the fixes above, `ruff` reports 12 errors and `mypy` 4, **all** inside
`core/image-prompt/image_prompt_core/` (`compiler.py`, `quality.py`, `router.py`,
`vision.py` are new and untracked; `__init__.py`, `corrector.py`, `schemas.py`
modified). mypy's file count moved 139 → 143 during this session: that package is
being written concurrently and is mid-flight.

The only Python touched by this QA pass is
`packages/shared/platform_shared/storage.py`, which passes both gates on its own:

```
ruff check packages/shared/platform_shared/storage.py   All checks passed!
mypy       packages/shared/platform_shared/storage.py   Success: no issues found
```

Re-run the full gates once the image-prompt work settles. The running containers are
built from baked images and are unaffected by these unstaged sources.

---

## 7. Found while topping up the QA workspace

### QA-015 — S3 — Admin actions use native `window.prompt()` / `alert()`
`Adjust credits`, `Change plan`, `Change platform role` collect their value with
`window.prompt()`, and `Metadata probe` reports through `alert()`. The polished
`adminConfirmDialog` is used for the reason step immediately afterwards, so the flow
is half native, half designed.

Consequences: the buttons silently do nothing wherever native dialogs are suppressed
(embedded views, automation, some enterprise policies) — `prompt()` returns null,
`Number(null)` is 0, and the handler returns early with no feedback. It is also
exactly the developer-grade UI the demo brief says to keep off camera.

Suggested: extend `adminConfirmDialog` with an optional typed input and route these
three through it. Left to Codex — it is their component.

### QA-016 — S3 — Most destructive action is the most prominent
In the user detail drawer, `Suspend user` renders as a solid red button in first
position, ahead of `Adjust credits` / `Change plan` / `Change platform role`. Same
family as QA-010.

## 8. Demo budget

`ui-rebuild-20260825@example.com` (workspace "Rebuild QA") topped up **50 → 3,000 CR**
through Admin → Users → Adjust credits. Ledger row `delta=2950, before=50, after=3000`
and audit row `CREDITS_ADJUSTED` both written. Ceiling agreed with the user: **USD 30**
(1 CR = $0.01, so 3,000 CR is exactly the ceiling — the credit balance *is* the cap).

---

## 9. Live generation smoke test — blocked by design

One real image generation was attempted (`seedream-5-0`, quoted 4 CR) to validate the
pipeline before planning demos. Three separate findings came out of it.

### QA-017 — S1 — FREE plan denies image generation outright
`entitlement_core/admission.py:76` raises
`FREE image generation is unavailable until a server-configured image role is enabled`
for any image request on a FREE workspace. Topping up credits does not help — the gate
is the plan tier, not the balance.

Resolved for QA by moving the workspace **FREE → PRO** through Admin → Users →
Change plan (reason recorded, audit row written). Worth deciding whether the Create
page should surface this as an upgrade prompt rather than a failure toast.

### QA-018 — S2 — The UI promises the model is never substituted; for images it is
On a paid plan the admission path ignores the client's model and resolves the
server-owned `ModelRole.IMAGE_GENERATION`:

```
selected  = model_roles.resolve(project_id, image_role, ...)
admitted.provider = selected.provider
admitted.model    = selected.provider_model_id
```

Selecting `Seedance · seedream-5-0` (quoted **4 CR · $0.04**) produced a job on
`openrouter · openai/gpt-image-2` charged at **15 CR**. Meanwhile the UI says, in
three places:

- inspector — "The model you pick is never silently swapped."
- submit toast — "Submitted. Your model choice is not substituted."
- marketing `/models` — "You can always override. You are never silently switched."

So the estimate is wrong, the attribution is wrong, and the copy is wrong. Either the
image path should honour the selection (as video does) or the UI must stop offering a
choice it does not have and quote the server-resolved model. This is a product
decision — flagged, not changed.

### QA-019 — S3 — Credits pill went stale after a refunded failure *(fixed)*
The job failed pre-submission and the ledger refunded correctly
(`refunded_credits=15, status=REFUNDED, balance_after=3000`), but the header pill kept
showing `2,985 CR` because `refreshPassengerJob` never re-read the balance. Fixed:
it now calls `loadCredits()` after polling.

### QA-020 — S1 (for demos) — Live generation needs a canary permit
```
status=FAILED  submission_state=NOT_SENT
error_code=LIVE_CANARY_DENIED
"no active live canary permit matches the server-selected provider/model"
```
With `PROVIDER_MODE=live`, every provider/model needs an active permit created through
`POST /internal/live-canary-permits` with `PLATFORM_API_KEY`, bounded by
`max_requests`, `max_cost_usd`, `expires_at`, and requiring `explicit_confirmation`.

`submission_state=NOT_SENT` — nothing reached the provider, **no real money was
spent**, and the reservation was refunded. The spend guard did its job.

No permits have been issued. Doing so authorises real provider spend and needs an
explicit decision on bounds per provider/model.

---

## 10. QA-018 resolved — image is routed, video may be named

Contract change, replacing the earlier "honour every named model" fix.

**Image.** The Create UI no longer offers image models at all; it offers a creative
task (Auto / Character / Product / Commercial / Scene / Beauty & fashion / Character
from reference), reusing the existing `ImageTaskType` taxonomy rather than inventing
one. The router resolves the target inside admission, *before* the quote and before
the credit reservation, so a figure shown to the user can only ever belong to the
model that runs. A request naming an image model is refused with 400.

**Video.** Model selection stays, defaulting to `Auto — Recommended`. Auto routes.
A named model is used verbatim for pricing, the stored job, the provider submission
and the billing, or the request fails with an explicit reason — never a substitution.

**Autopilot.** Unchanged and still role-controlled; the Director inspector exposes
camera/lighting only, no model picker.

Verified in the browser against the real API, with the resulting rows:

```
image | openrouter | openai/gpt-image-2 | quoted=15 | selection=ROUTER | task=portrait
video | seedance   | seedance-2.5       | quoted=44 | selection=MANUAL | task=-
```

The manual video job matched the 44 CR the UI had quoted before submit. Both jobs
then stopped at `LIVE_CANARY_DENIED` with `submission_state=NOT_SENT` and were
refunded; balance is back to 3,000 CR. No provider money was spent.

---

## 11. QA-009 and QA-013 closed, and the canary that follows them

### QA-009 — resolved — the probe's own verdict now wins

The two sources of truth were never really in disagreement; the mapping was
throwing half the answer away. Every adapter already reports a missing transport
identically —

```python
ProviderHealth(False, "NOT_CONFIGURED", {"status": "NOT_CONFIGURED", ...})
```

— so `ok=False` covers two different facts: *this provider is broken* and *this
provider was never wired up*. `"HEALTHY" if health.ok else "DOWN"` collapsed both
into red.

Fixed by reading the structured verdict instead of the boolean, in one helper used
at all three sites that map a probe (`dashboard` counts, `GET /admin/providers`,
`POST /admin/providers/{p}/probe`):

```python
def _probe_status(health: ProviderHealth) -> str:
    if health.ok:
        return "HEALTHY"
    reported = str(health.metadata.get("status") or health.detail or "").strip().upper()
    return "NOT_CONFIGURED" if reported == "NOT_CONFIGURED" else "DOWN"
```

This is the QA log's option 2, and it is the right one rather than a coin flip:
`is_configured()` is a synchronous registry check — *is a real adapter wired in* —
and cannot become a transport probe. `google_flow`'s own `capability_configured`
depends on a browser worker being connected right now, which is a probe-time fact
by construction.

`detail` also stopped repeating the badge: a probe whose detail is the bare token
`NOT_CONFIGURED` renders as "No generation transport is configured".

No frontend change was needed — `admin.css` already styles `NOT_CONFIGURED` grey
(`#86868f` on `#17171a`). Two regression tests pin both halves: the unconfigured
probe must be grey, and a genuinely failed probe must stay red.

**The running containers are built images with no source mount, so the console
still shows the old badge until `docker compose up -d --build api worker`.**

### QA-013 — the rule is written; applying it needs one operator command

The bucket has no CORS configuration at all — confirmed, not inferred:

```
get_bucket_cors → NoSuchCORSConfiguration: The CORS Configuration does not exist.
```

`scripts/configure_object_storage_cors.py` derives the rule from what the platform
actually sends rather than from a wildcard: origins from `WEB_ORIGINS`, and the
request headers `S3CompatibleStorage.presigned_upload` binds into the signature.
It prints the plan by default, refuses to clobber an existing configuration without
`--force`, and is a no-op when the rule is already in place.

```
AllowedOrigins  http://localhost:3000, http://127.0.0.1:18081
AllowedMethods  PUT, GET, HEAD
AllowedHeaders  content-type, content-md5, x-amz-checksum-sha256
ExposeHeaders   ETag, x-amz-checksum-sha256, x-oss-request-id
MaxAgeSeconds   3000
```

`--apply` was refused by the local permission classifier, not by OSS. Run:

```bash
uv run python scripts/configure_object_storage_cors.py --apply
uv run python scripts/verify_object_storage.py
```

Note this blocks **browser** uploads only. Server-side and presigned transfers
already pass (`[ok] client transfer`), so the canary below is not waiting on it.

### The closed-loop canary — `scripts/live_canary.py`

QA-018 and QA-020 established that everything up to the live gate works: the
browser produced `image | openrouter | openai/gpt-image-2 | quoted=15 CR` and
`video | seedance | seedance-2.5 | quoted=44 CR`, both stopped at
`LIVE_CANARY_DENIED` with `submission_state=NOT_SENT`, and both were refunded.
What has never run is the other side of that gate.

`scripts/live_canary.py` runs it, one smallest-approved request at a time, and
checks each stage against the thing it claims rather than against the stage before
it — the quote against `POST /api/pricing/estimate`, the reservation against
`workspace_credit_entries`, the transfer against a `HEAD` on the real bucket, the
debit against the append-only `workspace_credit_events` trail. A run that ends
`COMPLETED` with no settled entry fails here even though the picture came back.

It submits through `POST /api/passenger/generate`, the endpoint the Create canvas
uses, so what gets proven is the path a paying user takes. The image target names
no model, because image targets are router-owned (QA-018); the permit is minted for
the model the router is expected to pick and then checked against the one it did.

| Command | Spends |
| --- | --- |
| `live_canary.py image` | nothing — prints the plan and its cost |
| `live_canary.py image --confirm-spend` | one 1:1 image inside a 5-request / USD 3 / 2h permit |
| `live_canary.py video --confirm-spend` | one 4s 720p clip inside a 5-request / USD 5 / 2h permit |
| `live_canary.py video --failure-drill` | nothing — reserves, then lets the gate refuse it |

The drill is how credit release and error mapping get proven without paying: no
permit is minted, so the request is priced and reserved and then refused at the
live gate with `submission_state=NOT_SENT`, and the reservation has to come back.

Operator inputs, exported into the shell and never committed:

```
CANARY_ACCESS_TOKEN   bearer token for a workspace user holding credits
CANARY_PROJECT_ID     a project that user may write to
```

The script deliberately does not create either. Account operations belong to the
operator running it.

### QA-021 — S1 — Admin dashboard stranded a PostgreSQL backend on every load

Found by running the PostgreSQL half of the gate, not by reading the code. The
suite stopped dead for 13 minutes on:

```
pid 19679  active               DROP SCHEMA "t_91ee…" CASCADE      wait: Lock/relation
pid 19680  idle in transaction  SELECT generation_jobs.status, …   age: 771s
```

`GET /api/admin/dashboard` defines `job_window()` inside its
`with container.database.session() as session:` block but *called* it twice from
the response literal below that block. `Database.session()` ends in
`db.close()`, and a query issued on a closed `Session` does not fail — SQLAlchemy
quietly begins a **new** transaction on a **new** pooled connection, which
nothing then commits or closes.

So every dashboard load leaked one PostgreSQL backend, parked `idle in
transaction`, holding the `ACCESS SHARE` locks its `SELECT` took. Enough loads
exhaust the pool; one is enough to block DDL, which is how the test suite's own
`DROP SCHEMA … CASCADE` came to wait forever.

SQLite hid it completely — the same code passes there and always has. This is the
fourth defect the PostgreSQL half has caught that SQLite could not see.

Fixed by computing both panels inside the session block. An AST sweep of every
non-test module found no second instance of the shape (a closure defined inside a
session block and called outside it). A regression test asserts the invariant on
either engine:

```python
assert container.database.engine.pool.checkedout() == 0
```
