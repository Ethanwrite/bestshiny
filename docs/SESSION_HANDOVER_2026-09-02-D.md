# Session handover — 2026-09-02 D (the director writes: skill-driven turns, screenplay, locks, lineage)

**This is the entry point for the next session.** It records the "Create with BestShiny Director"
overhaul on branch `claude/bestshiny-director-workflow-1a6b59` (worktree
`.claude/worktrees/bestshiny-director-workflow-1a6b59`), committed as **`23f248d`**. Everything
below was run and read back unless it is listed under §6 as unverified. The previous entry point,
[`SESSION_HANDOVER_2026-09-02-C.md`](SESSION_HANDOVER_2026-09-02-C.md), described production before
this work. **Production was deployed from this branch on 2026-09-03 ≈01:08Z: `DEPLOYED_SHA =
c3b184b`, `.prev = 86987c3`, alembic `0070`, verified (see `docs/DEPLOYMENT.md` §6).** The PR
against `main` is open; after it merges, redeploy from `main` or record the squash SHA.

**The dev stack (`ai-director-platform` compose, api :8080 / web :3000) was rebuilt from `23f248d`
on 2026-09-02** and its database upgraded to `0070_creative_director_screenplay` by the api
container's own `alembic upgrade head`; running image ids equal the built ones, zero tracebacks.
An earlier round of browser testing had been done against the *previous* dev images (built from
`86987c3`), which is why it still showed the rule-based director. Dev runs `PROVIDER_MODE=live`
under the daily budget breaker, so every director turn and screenplay draft there is a paid
opus-5 call (turns were ≈USD 0.002 each in production; a 6000-token screenplay draft is
considerably more - watch `GET /internal/production-budget`).

---

## 1. What changed, in one screen

| Area | Before | Now |
| --- | --- | --- |
| DIRECTOR prompt | Hard-coded extraction prompt | `SkillRegistry.resolve("director")` is the system prompt; Skill version + content hash recorded on every director turn and screenplay revision, with the `ModelExecutionRecord` id |
| Model context | `known_fields` + last 12 turns | Ordered conversation (questions travel with replies), structured brief, per-field provenance, question states, user-established facts, stage, latest message; long conversations condensed on record (`context_json`) |
| Model output | `{"reply","fields"}` fill-empty merge | Validated `DirectorTurnResult`: message + SET/REPLACE/UPSERT/REMOVE/KEEP operations with evidence and confidence, answered/skipped codes, unresolved questions (≤3), assumptions, creative notes |
| Corrections | Impossible (fill-empty) | REPLACE/REMOVE honoured only on the user's words; inference never overwrites a user fact; characters merge by normalized name; `POST …/brief/edit` for direct edits (same provenance path) |
| Questions | "asked" ⇒ never asked again; proposable when nothing critical *unasked* | Per-code state machine; ≤3 per turn; re-ask allowed; CRITICAL must be ANSWERED or ASSUMPTION_ACCEPTED; CLARIFYING cannot be approved; assumptions must be confirmed — all enforced in `approve_brief` (409 + reason code) |
| Creation | `BeatPlanner` scaffold + preset lines ("你终于来了") as the normal output | DIRECTOR model writes versioned `Screenplay` revisions (treatment, hook, invariants/variables, characters+relationships, scenes, beats, dialogue, one-action ShotIntents, start/end/gaze, continuity obligations, product claims, required copy, unresolved). Scaffold only as labelled DETERMINISTIC degradation; approving it needs `accept_deterministic` |
| Key visuals | From the brief at approval; one row per key | From brief + approved screenplay; anchors versioned by `prompt_hash` (old rows SUPERSEDED, never re-used); required vs optional; optional may be skipped on record |
| Visual bible | Proposable with any anchors | Requires every required anchor READY and optional ones terminal; lock runs `CharacterIdentityService.confirm_identity` per character + STYLE asset version promoted and `ProjectStyleService.lock`; failure recorded in `lineage_json`, bible stays DRAFT, compile blocked |
| Compile | Beats from scaffold; script derived | Beats from the approved screenplay; beat edits become a new APPROVED `USER_EDIT` revision; the exact revision's script is compiled; `creative_shot_lineage` per shot; obligations from the screenplay opened in the ledger |
| Transactions | User turn committed before the model call | One transaction per round; crash ⇒ nothing written, no FREE round spent; `client_turn_id` replay |
| Frontend | Read-only brief, manual refresh | Brief editor, question/assumption panel, screenplay review/redraft/edit/approve, per-anchor retry/skip/regenerate-with-direction/use-my-image before the lock, backoff polling that stops on page/session change or terminal state, bible lineage, beat/shot editing sent through the existing `beats` parameter. Feel: a thinking bubble appears the moment a message is sent, the director's reply is then typed out chunk by chunk, a fresh screenplay unfolds section by section with its premise typed, key visuals render with a moving sheen, and a generating shot on the Director page shows an animated stage. This is progressive display of a completed reply — the model runtime returns one JSON object, it is not token streaming from the provider |

## 2. Files

- Core: `core/creative-director/creative_director_core/{schemas,brief,director_context,screenplay,service,beats,__init__}.py`
  (`director_context.py` and `screenplay.py` are new).
- Domain: `packages/domain/production_domain/models.py` (creative section: new statuses, columns,
  `CreativeScreenplayRevision`, `CreativeShotLineage`).
- Migration: `migrations/versions/0070_creative_director_screenplay.py`;
  `REQUIRED_SCHEMA_REVISION = "0070_creative_director_screenplay"`.
- API: `apps/api/video_platform_api/creative_routes.py` (new endpoints below), `container.py`
  (passes `skills`, `characters`, `styles`, `asset_registry` to the service).
- Web: `apps/web/app.js` (creative section rewritten; `request()` reads structured 409 details;
  `switchPage` stops the poll), `apps/web/index.html`, `apps/web/styles.css`.
- Tests: `tests/test_creative_director.py` (rewritten, 35 tests).
- Docs: `CURRENT_ARCHITECTURE.md` (creative section), `docs/OPEN_ISSUES.md` (§2.38 fixed, §2.46),
  `README.md`, this file, `HANDOFF.md` pointer.

## 3. State machine

```
INTAKE → CLARIFYING ⇄ BRIEF_PROPOSED → BRIEF_APPROVED → SCREENPLAY_PROPOSED → SCREENPLAY_APPROVED
      → VISUALS_IN_PROGRESS → BIBLE_PROPOSED → BIBLE_LOCKED → BEATS_PROPOSED → COMPILED
      (ABANDONED from anything but COMPILED)
```

Per question: `UNASKED → ASKED → ANSWERED | SKIPPED_BY_USER | ASSUMPTION_ACCEPTED` (an answer the
user removes reopens the question). Per anchor: `PENDING → GENERATING → READY | FAILED → SKIPPED`
(optional only) and `SUPERSEDED` when the content changed. Per bible: `DRAFT → LOCKED | SUPERSEDED`
with `lineage_json.lock_status ∈ {NOT_LOCKED, PARTIAL, FAILED, LOCKED}`. Screenplay and brief
revisions: `PROPOSED → APPROVED | SUPERSEDED`.

## 4. Endpoints added or changed

```
POST /v1/creative/sessions                       body + client_turn_id; reply carries reasoner, reason_codes,
                                                 assumptions, blocking, retryable, turn_sequence, replayed
POST /v1/creative/sessions/{id}/messages         same reply; typed "批准" approves under the same constraints
POST /v1/creative/sessions/{id}/brief/edit       {operations:[{op,path,value,evidence}]}  → revision + applied/rejected
POST /v1/creative/sessions/{id}/brief/questions  {code, action: ACCEPT_ASSUMPTION|SKIP, value?}
POST /v1/creative/sessions/{id}/brief/approve    {revision, accept_assumptions} → brief + screenplay draft
POST /v1/creative/sessions/{id}/screenplay/propose {notes}      (redraft with the model)
POST /v1/creative/sessions/{id}/screenplay/edit    {content}    (user revision, validated)
POST /v1/creative/sessions/{id}/screenplay/approve {revision, accept_deterministic} → anchors + executions
POST /v1/creative/sessions/{id}/visuals/anchors/{anchor_id}/skip {reason}
POST /v1/creative/sessions/{id}/visuals/anchors/{anchor_id}/regenerate {direction}  (new version, generated anew)
POST /v1/creative/sessions/{id}/visuals/anchors/{anchor_id}/replace {media_asset_id} (user's uploaded image, READY)
POST /v1/creative/sessions/{id}/bible/approve    locks identities + style (signed-in user required)
GET  /v1/creative/shots/{shot_id}/lineage
```
Refusals are `409 {"detail": {"message", "reason_code", "retryable", …}}`.

## 5. Which stages the model owns, which are deterministic

| Stage | Model (DIRECTOR through `ModelRoleRuntime`, Skill as system prompt) | Deterministic |
| --- | --- | --- |
| Dialogue turn | Reply, brief operations, assumptions, unresolved questions, creative notes | Operation validation and provenance rules; question state machine; gap analysis (which codes may be asked, proposability); regex fill of still-empty fields from the user's literal words; the fallback sentence when the model is unavailable |
| Brief approval | — | Constraint checks, ASSUMPTION_ACCEPTED provenance |
| Screenplay | Treatment, invariants/variables, characters, scenes, beats, dialogue, shot intents, states, obligations | Schema + cross-reference validation, rendering to the compiler's line vocabulary, the labelled scaffold when the model is unavailable |
| Key visuals | — | Anchor derivation (brief + screenplay), versioning, prompt composition; generation via admission/credits/router/gateway |
| Visual bible / locks | — | Identity and style services |
| Beats / compile | — (edits are the user's) | Materialization from the approved screenplay, narrative compiler, frame anchor planner, lineage, ledger |

## 6. Gates and what was *not* verified

Gates on the final tree (2026-09-02, run from the worktree on the main venv, detached):

| Gate | Result |
| --- | --- |
| `pytest` (SQLite half) | 1409 passed, 12 skipped, exit 0 (8m14s) before the image-change/UX round; **1411 passed, 12 skipped, exit 0 (6m28s) on `23f248d`** |
| `pytest --database=postgres` | 1414 passed, 7 skipped, exit 0 (19m37s) before the image-change/UX round; on `23f248d` the creative, free-tier and migration suites re-ran on PostgreSQL: 67 passed, exit 0 |
| `ruff check .` | clean |
| `mypy` (repo config) | clean, 199 source files |
| `vite build` (apps/web) | built |
| Browser smoke (test-mode API on 18080 + built bundle on 18081) | idea → brief (provenance chips, editor: duration edit recorded as "you edited") → approval → labelled deterministic scaffold refused until accepted → key visuals (failed anchors with retry/skip, required not skippable) → media bound by script → bible drafted → lock refused under the development bypass; as a signed-in user: bible locked (style lock created, identity v1), beats drafted, a line and a duration edited, compiled 7 shots from screenplay r2, Director page shows EP01 with 7 shots |

Not verified here:

1. **No live DIRECTOR call** against the new JSON contracts (mock provider mode). A live model may
   wrap JSON in prose or return an invalid screenplay; both degrade to the labelled scaffold. First
   thing after deploy: one paid turn and one paid screenplay draft on dev, then read
   `creative_turns.reason_codes` / `creative_screenplays.reason_codes`.
2. **No browser session** exercised the new page; the bundle builds and the JS parses.
3. Multi-word Latin names become one hyphenated script token (`Lin-Jin`); CJK names over four
   characters do not parse as actors in the narrative compiler.
4. One style lock per project: a second session inherits it (recorded as `STYLE_LOCK_INHERITED`).
5. The dev stack (`ai-director-platform` compose) still runs `86987c3`; it needs a rebuild and
   `alembic upgrade head` (to `0070`) to exercise this branch.
