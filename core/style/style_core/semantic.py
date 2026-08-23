"""The second style layer: what a colour/edge histogram structurally cannot see.

`LocalStyleDescriptor` summarises a frame as colour, tonal, saturation, edge and
spatial statistics. That is a good detector for the failures it was built for —
a grade shift, a contrast collapse, a palette walking away over an episode — and
it is deterministic, free and offline, which is why it stays.

It is also blind in a specific, predictable way. Rendering *medium* barely moves
those statistics: oil paint and a 3D render of the same scene under the same
palette produce nearly the same histogram, as do 35mm and a phone camera. A
series can therefore drift from illustrated to photographic while every frame
scores near 1.0.

A multimodal embedding sees medium, brushwork and photographic language, and is
correspondingly weak where the descriptor is strong — a regrade that preserves
the medium reads as "same style" to it. Neither layer subsumes the other, so
both run and both must pass.

This module owns only the boundary. The model behind it is resolved through
`ModelRole.STYLE_SEMANTIC_EMBEDDING`, so it obeys the same role/live/canary
controls as every other model call, and nothing here decides which model runs.
"""

from __future__ import annotations

import asyncio
import base64
from threading import Thread
from typing import Any, Protocol, runtime_checkable


class SemanticStyleUnavailable(RuntimeError):
    """The semantic layer could not produce evidence for this candidate.

    Raised rather than returning a neutral score: a missing second opinion is
    not a passing one, and the caller turns this into REVIEW_REQUIRED.
    """


@runtime_checkable
class SemanticStyleEmbedder(Protocol):
    """Embeds image bytes into a style-comparable vector space."""

    @property
    def model(self) -> str: ...

    @property
    def provider(self) -> str: ...

    def embed_images(self, images: list[bytes], *, project_id: str) -> list[list[float]]:
        """One vector per input image, in the same order.

        Raises ``SemanticStyleUnavailable`` when no evidence can be produced.
        """
        ...


class ModelRoleSemanticStyleEmbedder:
    """Resolves the semantic style model through the ordinary role runtime.

    Deliberately thin: it converts image bytes to the transport's input shape
    and nothing else. Model choice, credentials, live gating and the canary
    permit are all the role runtime's business, not this module's.
    """

    version = "semantic-style-embedder-v1"

    def __init__(self, model_roles: Any, *, mime_type: str = "image/png", dimensions: int = 1024):
        self.model_roles = model_roles
        self.mime_type = mime_type
        self.dimensions = dimensions
        self._resolved_model = ""
        self._resolved_provider = ""

    @property
    def model(self) -> str:
        return self._resolved_model or "unresolved"

    @property
    def provider(self) -> str:
        return self._resolved_provider or "unresolved"

    def embed_images(self, images: list[bytes], *, project_id: str) -> list[list[float]]:
        if not images:
            raise SemanticStyleUnavailable("no frames were supplied for semantic style embedding")
        from model_registry_core import ModelRole

        inputs = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{self.mime_type};base64,{base64.b64encode(payload).decode()}"
                },
            }
            for payload in images
        ]
        try:
            execution = _run_blocking(
                self.model_roles.execute_embeddings(
                    project_id,
                    inputs=[{"content": [item]} for item in inputs],
                    role=ModelRole.STYLE_SEMANTIC_EMBEDDING,
                    parameters={"dimensions": self.dimensions},
                )
            )
        except Exception as exc:
            raise SemanticStyleUnavailable(f"semantic style model is unavailable: {exc}") from exc

        resolved = getattr(execution, "resolved_model", None)
        if resolved is not None:
            self._resolved_model = str(getattr(resolved, "provider_model_id", "") or "")
            self._resolved_provider = str(getattr(resolved, "provider", "") or "")
        vectors = _vectors_from_response(getattr(execution, "response", None) or {})
        if len(vectors) != len(images):
            raise SemanticStyleUnavailable(
                f"semantic style model returned {len(vectors)} vectors for {len(images)} frames"
            )
        return vectors


def _run_blocking(coroutine: Any) -> Any:
    """Run one coroutine from synchronous code, on this loop or beside it.

    Style evaluation is synchronous and provider capabilities are not. This is
    the same bridge Narrative Memory uses; duplicating the three lines is
    cheaper than making the style gate async for one call.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: list[Any] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:  # pragma: no cover - re-raised on the caller thread
            errors.append(exc)

    thread = Thread(target=run, name="semantic-style-embedding", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    if not result:  # pragma: no cover - defensive invariant
        raise SemanticStyleUnavailable("semantic style execution returned no result")
    return result[0]


def _vectors_from_response(response: dict[str, Any]) -> list[list[float]]:
    entries = response.get("data")
    if not isinstance(entries, list):
        raise SemanticStyleUnavailable("semantic style response contained no embedding data")
    vectors: list[list[float]] = []
    for entry in entries:
        raw = entry.get("embedding") if isinstance(entry, dict) else None
        if not isinstance(raw, list) or not raw:
            raise SemanticStyleUnavailable("semantic style response contained an empty embedding")
        try:
            vectors.append([float(value) for value in raw])
        except (TypeError, ValueError) as exc:
            raise SemanticStyleUnavailable("semantic style embedding is not numeric") from exc
    return vectors


__all__ = [
    "ModelRoleSemanticStyleEmbedder",
    "SemanticStyleEmbedder",
    "SemanticStyleUnavailable",
]
