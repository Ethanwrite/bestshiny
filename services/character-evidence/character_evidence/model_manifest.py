from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODEL_ROOT = Path("/models")
SOURCE_ARTIFACTS = {
    "multi_object_tracking": Path("/opt/YOLOX/yolox/tracker/byte_tracker.py"),
}


@dataclass(frozen=True)
class ModelManifest:
    payload: dict[str, Any]

    @property
    def version(self) -> str:
        return str(self.payload["manifest_version"])

    @property
    def pipeline_version(self) -> str:
        return str(self.payload["pipeline_version"])

    @property
    def threshold_version(self) -> str:
        return str(self.payload["threshold_version"])

    @property
    def by_role(self) -> dict[str, dict[str, Any]]:
        return {str(item["role"]): dict(item) for item in self.payload["models"]}

    def provenance(self) -> dict[str, dict[str, Any]]:
        return self.by_role


def manifest_path() -> Path:
    return Path(__file__).resolve().parents[1] / "character_evidence_model_manifest.json"


def load_manifest(path: Path | None = None) -> ModelManifest:
    payload = json.loads((path or manifest_path()).read_text(encoding="utf-8"))
    required = {
        "role",
        "model_name",
        "model_version",
        "source_repository",
        "source_revision",
        "artifact",
        "sha256",
        "threshold_version",
    }
    roles: set[str] = set()
    for item in payload.get("models", []):
        missing = required - set(item)
        if missing:
            raise ValueError(f"model manifest entry is missing {sorted(missing)}")
        role = str(item["role"])
        if role in roles:
            raise ValueError(f"duplicate model manifest role: {role}")
        roles.add(role)
        digest = str(item["sha256"])
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(f"invalid SHA256 for model role {role}")
        if not item.get("loaded_at_build"):
            raise ValueError(f"production model is not build-cached: {role}")
    expected = {
        "person_detection",
        "multi_object_tracking",
        "face_detection",
        "face_identity",
        "appearance_encoding",
    }
    if roles != expected:
        raise ValueError(f"model manifest roles do not match production pipeline: {sorted(roles)}")
    return ModelManifest(payload)


def artifact_path(role: str, entry: dict[str, Any]) -> Path:
    if role in SOURCE_ARTIFACTS:
        return SOURCE_ARTIFACTS[role]
    return MODEL_ROOT / str(entry["artifact"])


def verify_artifacts(manifest: ModelManifest) -> None:
    for role, entry in manifest.by_role.items():
        path = artifact_path(role, entry)
        if not path.is_file():
            raise RuntimeError(f"required build-cached model artifact is missing: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise RuntimeError(f"model artifact SHA256 mismatch for {role}: {path}")
