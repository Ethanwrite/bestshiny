from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from threading import Thread
from typing import Literal, Protocol

import httpx
from entitlement_core import ModelRoleRuntime
from production_domain.models import EmbeddingEvidence, ModelExecutionRecord
from provider_sdk import LiveProviderGate, LiveProviderSettings

from .schemas import EmbeddingProvenance, MultimodalContent

EmbeddingInputType = Literal["query", "document"]


class MemoryEmbeddingUnavailable(RuntimeError):
    """The optional vector-memory backend could not produce trustworthy evidence."""


@dataclass(frozen=True)
class EmbeddingVector:
    values: list[float]
    provenance: EmbeddingProvenance


class EmbeddingProvider(Protocol):
    @property
    def provenance(self) -> EmbeddingProvenance: ...

    def embed(self, content: MultimodalContent, *, input_type: EmbeddingInputType) -> list[float]: ...

    def embed_with_provenance(
        self,
        content: MultimodalContent,
        *,
        input_type: EmbeddingInputType,
        project_id: str,
    ) -> EmbeddingVector: ...


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

    def embed(self, content: MultimodalContent, *, input_type: EmbeddingInputType) -> list[float]:
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

    def embed_with_provenance(
        self,
        content: MultimodalContent,
        *,
        input_type: EmbeddingInputType,
        project_id: str,
    ) -> EmbeddingVector:
        del project_id
        values = self.embed(content, input_type=input_type)
        return EmbeddingVector(
            values,
            self.provenance.model_copy(update={"input_type": input_type}),
        )


class VoyageMultimodalEmbeddingProvider:
    endpoint = "https://api.voyageai.com/v1/multimodalembeddings"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "voyage-multimodal-3.5",
        dimension: int = 512,
        timeout_seconds: float = 30.0,
        transport_settings: LiveProviderSettings | None = None,
    ):
        if not api_key:
            raise ValueError("Voyage API key is required")
        if dimension not in {256, 512, 1024, 2048}:
            raise ValueError("Voyage Matryoshka dimension must be 256, 512, 1024, or 2048")
        self.api_key = api_key
        self.model = model
        self.dimension = dimension
        self.timeout_seconds = timeout_seconds
        self.live_gate = LiveProviderGate(transport_settings or LiveProviderSettings())

    @property
    def provenance(self) -> EmbeddingProvenance:
        return EmbeddingProvenance(
            provider="voyage",
            model=self.model,
            dimension=self.dimension,
            input_type="document",
        )

    def embed(self, content: MultimodalContent, *, input_type: EmbeddingInputType) -> list[float]:
        # A configured key alone is never authority to make a paid HTTP call.
        self.live_gate.assert_live_allowed()
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

    def embed_with_provenance(
        self,
        content: MultimodalContent,
        *,
        input_type: EmbeddingInputType,
        project_id: str,
    ) -> EmbeddingVector:
        del project_id
        values = self.embed(content, input_type=input_type)
        return EmbeddingVector(
            values,
            self.provenance.model_copy(update={"input_type": input_type}),
        )


class ModelRoleEmbeddingProvider:
    """Project-scoped Narrative Memory adapter for ``ModelRoleRuntime``.

    Memory's public service is synchronous today, while provider capabilities are
    asynchronous.  The bridge runs the coroutine directly when no event loop is
    active and otherwise isolates it in a short-lived daemon thread.  Business
    code never receives or calls a Voyage/OpenRouter client directly.
    """

    def __init__(self, runtime: ModelRoleRuntime, *, dimension: int = 512):
        if dimension not in {256, 512, 1024, 2048}:
            raise ValueError("embedding dimension must be 256, 512, 1024, or 2048")
        self.runtime = runtime
        self.dimension = dimension

    @property
    def provenance(self) -> EmbeddingProvenance:
        # A project is required to resolve a role binding.  This property exists
        # only for protocol compatibility; the engine uses embed_with_provenance.
        raise MemoryEmbeddingUnavailable("project-scoped embedding provenance is required")

    def embed(self, content: MultimodalContent, *, input_type: EmbeddingInputType) -> list[float]:
        del content, input_type
        raise MemoryEmbeddingUnavailable("project_id is required for ModelRole embeddings")

    def embed_with_provenance(
        self,
        content: MultimodalContent,
        *,
        input_type: EmbeddingInputType,
        project_id: str,
    ) -> EmbeddingVector:
        pieces: list[dict[str, str]] = []
        if content.text:
            pieces.append({"type": "text", "text": content.text})
        pieces.extend({"type": "image_url", "image_url": url} for url in content.image_urls)
        pieces.extend({"type": "video_url", "video_url": url} for url in content.video_urls)
        try:
            execution = _run_blocking(
                self.runtime.execute_embeddings(
                    project_id,
                    inputs=[{"content": pieces}],
                    parameters={"dimensions": self.dimension, "input_type": input_type},
                )
            )
            vector = _extract_embedding(execution.response)
        except Exception as exc:
            raise MemoryEmbeddingUnavailable("model-role embedding execution failed") from exc
        if len(vector) < self.dimension:
            raise MemoryEmbeddingUnavailable(
                f"embedding response has {len(vector)} dimensions; expected at least {self.dimension}"
            )
        normalized = vector[: self.dimension]
        norm = math.sqrt(sum(item * item for item in normalized)) or 1.0
        values = [item / norm for item in normalized]
        provenance = EmbeddingProvenance(
            provider=execution.resolved_model.provider,
            model=execution.resolved_model.provider_model_id,
            dimension=len(values),
            input_type=input_type,
        )
        input_hash = hashlib.sha256(
            json.dumps(
                content.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        embedding_hash = hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("utf-8")).hexdigest()
        with self.runtime.database.session() as session:
            record = session.get(ModelExecutionRecord, execution.execution_record_id)
            if record is None:
                raise MemoryEmbeddingUnavailable("model execution evidence was not persisted")
            session.add(
                EmbeddingEvidence(
                    project_id=project_id,
                    model_definition_id=execution.resolved_model.definition_id,
                    model_execution_record_id=record.id,
                    input_hash=input_hash,
                    embedding_dimension=len(values),
                    embedding_hash=embedding_hash,
                    latency_ms=record.latency_ms,
                    cost_usd=record.actual_cost_usd,
                )
            )
        return EmbeddingVector(
            values,
            provenance,
        )


def _run_blocking(coroutine):  # type: ignore[no-untyped-def]
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: list[object] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:  # pragma: no cover - re-raised on caller thread
            errors.append(exc)

    thread = Thread(target=run, name="model-role-embedding", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    if not result:  # pragma: no cover - defensive invariant
        raise RuntimeError("embedding execution returned no result")
    return result[0]


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
