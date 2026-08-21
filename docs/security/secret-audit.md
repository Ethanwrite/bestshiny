# Secret Audit

Audit date: 2026-08-21

Scope: tracked repository, staged baseline diff, practical Git history search, local `.env*`
files, tests, documentation, logs/caches, and the ignored local `references/` tree. Values were
matched in memory and reported only by a truncated SHA-256 fingerprint; this document never
stores a credential value.

## Operator decision

The operator explicitly confirmed on 2026-08-21 that the currently configured Provider keys do
not require rotation. That decision overrides the Phase III draft's blanket
`ROTATION_REQUIRED` policy. The application still treats credentials as environment/runtime
secrets: they must not be committed, logged, embedded in fixtures, or used unless the live gates
and a bounded canary permit are satisfied.

## Findings

| Provider / type | Location | Redacted fingerprint | Classification | Action |
| --- | --- | --- | --- | --- |
| Runtime Provider credentials | Operator environment / conversation only; not found in tracked files or Git history | Not persisted by audit | `ACTIVE` by operator decision | No rotation. Keep outside Git and logs. |
| Google-compatible key-shaped value | Ignored local `references/` tree (six copies; not returned by `git ls-files`) | `sha256:a6d65898ecdb…` | Reference-only, outside repository baseline | Keep `references/` ignored. Replace with an environment placeholder before any future vendoring or redistribution. |
| Ark-shaped value | `tests/test_model_infrastructure.py` | `sha256:33d168a6fd38…` | Model identifier fixture, not a credential | No action. |
| RunAPI-shaped matches | Phase II tests | Multiple | Test function names / deterministic fake values, not credentials | No action. |
| `.env` files | Only `.env.example` exists in the project tree | N/A | Placeholders only | Continue ignoring `.env` and `.env.*` except `.env.example`. |
| Private keys, Google API keys, OpenRouter keys, RunAPI tokens, generic `sk-*` secrets | Tracked files and practical Git-history pattern scan | None found | Clear | No action. |

## Baseline controls verified

- `.gitignore` excludes `.env`, local databases, generated output, caches, and the local
  `references/` tree.
- `.dockerignore` excludes `.env*` (while retaining `.env.example`), Git metadata, local
  databases, generated output, secrets, private-key files, caches, and virtual environments.
- The baseline commit contains no `.env`, credential dump, cookie/session data, generated
  media, or local database backup.
- Provider transports keep credentials in process memory and redact request authorization from
  persisted job metadata.
- Ordinary tests force mock mode and close all live-provider gates.

## Repeatable audit procedure

1. Enumerate `.env*` files and confirm only `.env.example` is tracked.
2. Search the worktree with provider-specific token shapes, returning file paths only.
3. Hash matches in memory and inspect sanitized context; never print the complete match.
4. Run equivalent `git log -G` searches with `--name-only`.
5. Inspect staged paths and run `git diff --cached --check` before every release baseline.

This audit establishes repository hygiene; it does not claim that an external Provider's account
history or revocation state was independently verified.
