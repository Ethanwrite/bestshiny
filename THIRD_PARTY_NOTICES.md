# Third-party notices

## flow2api

- Source: https://github.com/TheSmallHanCat/flow2api
- Audited commit: `193008716e3cc57d6c22be418e134e7eabf84358`
- License: MIT
- Copyright: © 2025 TheSmallHanCat
- Adapted concepts/files: load-aware account selection and concurrency reservation from
  `src/services/load_balancer.py` and `src/services/concurrency_manager.py`; model/tier filtering from
  `src/core/account_tiers.py`; provider request/error behavior from `src/services/flow_client.py` and
  `src/services/generation_handler.py`.

## flowkit

- Source: https://github.com/crisng95/flowkit
- Audited commit: `66e859645fd14bd33f6ceb9ac143a3ff896c61d8`
- License: MIT
- Copyright: © 2026 tuannguyenhoangit-droid
- Adapted concepts/files: browser extension transport from `extension/`; worker recovery from
  `agent/worker/processor.py`; Flow transport from `agent/services/flow_client.py`; chaining and file-based skill
  structure from `agent/services/scene_chain.py` and `skills/`.

The complete upstream license texts are preserved in `licenses/flow2api-MIT.txt` and
`licenses/flowkit-MIT.txt`.

## flow-agent

- Source: https://github.com/kodelyx/flow-agent
- Audited commit: `113f17e7057a3195808edb71b5c4c3b6e234163d`
- License result: no license file found in the audited repository.
- Usage: reference implementation only. No source code copied. Persistent media, idempotency, late response,
  restart recovery and multi-worker behaviors were independently implemented.

