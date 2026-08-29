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

## Character Evidence model stack

- YOLOX-s 0.1.1rc0 — Megvii-BaseDetection/YOLOX, audited commit
  `e1052df71842031413f6030723c3607b839c80ce`, Apache-2.0.
- ByteTrack — FoundationVision/ByteTrack, audited commit
  `d1bf0191adff59bc8fcfeaa0b33d3d1642552a99`, MIT.
- YuNet 2026may — opencv/opencv_zoo, audited commit
  `47534e27c9851bb1128ccc0102f1145e27f23f98`, model directory licensed MIT.
- SFace 2021dec — opencv/opencv_zoo at the same audited commit, model directory licensed Apache-2.0.
- DINOv2-base (`dinov2_vitb14`) — facebookresearch/dinov2, audited commit
  `7764ea0f912e53c92e82eb78a2a1631e92725fc8`, Apache-2.0.

The exact artifact SHA-256 values and source revisions used by the image build are recorded in
`services/character-evidence/character_evidence_model_manifest.json`.

## flow-agent

- Source: https://github.com/kodelyx/flow-agent
- Audited commit: `113f17e7057a3195808edb71b5c4c3b6e234163d`
- License result: no license file found in the audited repository.
- Usage: reference implementation only. No source code copied. Persistent media, idempotency, late response,
  restart recovery and multi-worker behaviors were independently implemented.
