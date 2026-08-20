from __future__ import annotations

from .adapters import GrokAdapter, KlingAdapter, SeedanceAdapter, VeoAdapter, WanAdapter
from .base import VideoModelAdapter


class VideoAdapterRegistry:
    def __init__(self):
        self._adapters: dict[str, VideoModelAdapter] = {}
        for adapter in [KlingAdapter(), VeoAdapter(), SeedanceAdapter(), GrokAdapter(), WanAdapter()]:
            self.register(adapter)

    def register(self, adapter: VideoModelAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> VideoModelAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise LookupError(f"video model adapter not registered: {name}") from exc

    def names(self) -> list[str]:
        return sorted(self._adapters)
