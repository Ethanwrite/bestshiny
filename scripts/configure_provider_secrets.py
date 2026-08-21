#!/usr/bin/env python3
"""Interactively store provider credentials in the gitignored local .env.

Values are read with ``getpass`` so they are not echoed or passed on the
command line.  This helper intentionally leaves every live-call gate closed;
enabling paid traffic is a separate, explicit operational action.
"""

from __future__ import annotations

import argparse
import getpass
import os
import tempfile
from pathlib import Path

SECRET_FIELDS = (
    ("OPENROUTER_API_KEY", "OpenRouter API Key"),
    ("ARK_API_KEY", "Volcengine Ark API Key"),
    ("WAN_API_KEY", "Alibaba Model Studio / Wan API Key"),
    ("RUNAPI_API_KEY", "RunAPI API Key"),
    ("DEEPSEEK_API_KEY", "DeepSeek API Key"),
)

MODEL_FIELDS = (
    ("DOUBAO_MODEL_ID", "Doubao deployment/model ID"),
    ("SEEDANCE_MODEL_ID", "Seedance deployment/model ID"),
    ("WAN_CHAT_MODEL_ID", "Alibaba compatible chat model ID"),
    ("WAN2_7_T2V_MODEL_ID", "Wan 2.7 text-to-video model ID"),
    ("WAN2_7_I2V_MODEL_ID", "Wan 2.7 image-to-video model ID"),
    ("WAN2_7_R2V_MODEL_ID", "Wan 2.7 reference-to-video model ID"),
    ("RUNAPI_MODEL_ID", "RunAPI low-trust prompt model ID"),
    ("DEEPSEEK_MODEL_ID", "DeepSeek model ID"),
)

SAFE_DEFAULTS = {
    "PROVIDER_MODE": "mock",
    "ALLOW_LIVE_PROVIDER_CALLS": "false",
    "LIVE_PROVIDER_CONFIRMATION": "",
    "ALLOW_RUNAPI_EDGE_CALLS": "false",
    "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    "ARK_BASE_URL": "https://ark.cn-beijing.volces.com/api/v3",
    "WAN_OPENAI_BASE_URL": ("https://llm-v2t9buz4qi8osnqs.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"),
    "WAN_DASHSCOPE_BASE_URL": ("https://llm-v2t9buz4qi8osnqs.cn-beijing.maas.aliyuncs.com/api/v1"),
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
    "RUNAPI_BUDGET_USD": "10.00",
}


def _read_values(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value
    return lines, values


def _merge(lines: list[str], updates: dict[str, str]) -> list[str]:
    output: list[str] = []
    remaining = dict(updates)
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining:
        if output and output[-1]:
            output.append("")
        output.append("# Provider infrastructure (managed by configure_provider_secrets.py)")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    return output


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".env.", dir=path.parent, text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--skip-model-ids",
        action="store_true",
        help="Only collect secrets; leave provider deployment/model IDs unchanged.",
    )
    args = parser.parse_args()
    lines, existing = _read_values(args.env_file)
    updates = dict(SAFE_DEFAULTS)

    print("Provider keys are hidden while typing. Press Enter to keep an existing value.")
    for key, label in SECRET_FIELDS:
        suffix = " [already set]" if existing.get(key) else ""
        value = getpass.getpass(f"{label}{suffix}: ").strip()
        if value:
            updates[key] = value
        elif key in existing:
            updates[key] = existing[key]

    if not args.skip_model_ids:
        print("\nModel/deployment IDs are not API keys. Leave unknown IDs blank.")
        for key, label in MODEL_FIELDS:
            suffix = f" [{existing[key]}]" if existing.get(key) else ""
            value = input(f"{label}{suffix}: ").strip()
            if value:
                updates[key] = value
            elif key in existing:
                updates[key] = existing[key]

    merged = _merge(lines, updates)
    _atomic_write(args.env_file, "\n".join(merged).rstrip() + "\n")
    print(f"Saved {args.env_file} with mode 0600. Paid/live provider gates remain disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
