from __future__ import annotations

import json
from pathlib import Path

from .schemas import ModelCapabilityProfile


class ModelCapabilityRegistry:
    """Validated, reloadable model profiles backed by configuration files."""

    def __init__(self, config_root: Path):
        self.config_root = config_root
        self._profiles: dict[str, ModelCapabilityProfile] = {}
        self.reload()

    def reload(self) -> None:
        profiles: dict[str, ModelCapabilityProfile] = {}
        if not self.config_root.is_dir():
            raise FileNotFoundError(f"model capability directory not found: {self.config_root}")
        for path in sorted(self.config_root.glob("*.json")):
            profile = ModelCapabilityProfile.model_validate(json.loads(path.read_text(encoding="utf-8")))
            if profile.key in profiles:
                raise ValueError(f"duplicate model capability profile: {profile.key}")
            profiles[profile.key] = profile
        if not profiles:
            raise ValueError("model capability registry cannot be empty")
        self._profiles = profiles

    def all(self, *, include_disabled: bool = False) -> list[ModelCapabilityProfile]:
        return [
            profile for profile in self._profiles.values() if include_disabled or profile.status != "disabled"
        ]

    def get(self, model_id: str, provider: str | None = None) -> ModelCapabilityProfile | None:
        if provider:
            return self._profiles.get(f"{provider}:{model_id}")
        matches = [profile for profile in self._profiles.values() if profile.model_id == model_id]
        if len(matches) > 1:
            raise LookupError(f"model ID is ambiguous without provider: {model_id}")
        return matches[0] if matches else None

    def by_provider(self, provider: str) -> list[ModelCapabilityProfile]:
        return [profile for profile in self.all() if profile.provider == provider]

    def replace(self, profile: ModelCapabilityProfile) -> None:
        """Replace an in-memory profile; persistence remains an explicit admin operation."""

        self._profiles[profile.key] = profile
