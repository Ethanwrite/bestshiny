from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

import httpx

from .schemas import EmbeddingProvenance, MultimodalContent


class EmbeddingProvider(Protocol):
    @property
    def provenance(self) -> EmbeddingProvenance: ...

    def embed(self, content: MultimodalContent, *, input_type: str) -> list[float]: ...


class LocalTestEmbeddingProvider:
    """Deterministic lexical fallback for tests and offline development only.

    It is deliberately labelled local_test and must never be represented as a
    visual or identity judge.
    """

    def __init__(self, dimension: int = 512):
        self.dimension = dimension

    @property
    def provenance(self) -> EmbeddingProvenance:
        return EmbeddingProvenance(
            provider="local_test",
            model="deterministic-token-hash-v1",
            dimension=self.dimension,
            input_type="document",
        )

    def embed(self, content: MultimodalContent, *, input_type: str) -> list[float]:
        value = " ".join([content.text, *content.image_urls, *content.video_urls]).lower()
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", value)
        vector = [0.0] * self.dimension
        for token in tokens or ["empty"]:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = -1.0 if digest[4] & 1 else 1.0
            vector[index] += sign
        norm = math.sqrt(sum(item * item for item in vector)) or 1.0
        return [item / norm for item in vector]


class VoyageMultimodalEmbeddingProvider:
    endpoint = "https://api.voyageai.com/v1/multimodalembeddings"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "voyage-multimodal-3.5",
        dimension: int = 512,
        timeout_seconds: float = 30.0,
    ):
        if not api_key:
            raise ValueError("Voyage API key is required")
        if dimension not in {256, 512, 1024, 2048}:
            raise ValueError("Voyage Matryoshka dimension must be 256, 512, 1024, or 2048")
        self.api_key = api_key
        self.model = model
        self.dimension = dimension
        self.timeout_seconds = timeout_seconds

    @property
    def provenance(self) -> EmbeddingProvenance:
        return EmbeddingProvenance(
            provider="voyage",
            model=self.model,
            dimension=self.dimension,
            input_type="document",
        )

    def embed(self, content: MultimodalContent, *, input_type: str) -> list[float]:
        pieces: list[dict[str, str]] = []
        if content.text:
            pieces.append({"type": "text", "text": content.text})
        pieces.extend({"type": "image_url", "image_url": url} for url in content.image_urls)
        pieces.extend({"type": "video_url", "video_url": url} for url in content.video_urls)
        response = httpx.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "inputs": [{"content": pieces}],
                "model": self.model,
                "input_type": input_type,
                "truncation": True,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        vector = _extract_embedding(payload)
        # voyage-multimodal-3.5 is Matryoshka-compatible. The REST endpoint
        # currently returns the default vector, so retain its leading dimensions.
        if len(vector) < self.dimension:
            raise RuntimeError(
                f"Voyage returned {len(vector)} dimensions; configured dimension is {self.dimension}"
            )
        vector = vector[: self.dimension]
        norm = math.sqrt(sum(item * item for item in vector)) or 1.0
        return [item / norm for item in vector]


def _extract_embedding(payload: dict) -> list[float]:  # type: ignore[type-arg]
    data: Sequence | None = payload.get("data")
    if data and isinstance(data[0], dict):
        value = data[0].get("embedding")
        if isinstance(value, list):
            return [float(item) for item in value]
    embeddings = payload.get("embeddings")
    if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
        return [float(item) for item in embeddings[0]]
    raise RuntimeError("Voyage response did not contain an embedding")
