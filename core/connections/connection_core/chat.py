"""Chat completion over a connection: OpenAI-compatible, Anthropic Messages, and mock.

Each client normalizes one provider answer into ``ChatResult`` - the text,
the model that actually served it, why it stopped, and token usage - and
raises ``ProviderCallFailed`` with a stable code for everything else, so the
canvas can show the user the provider's own words next to the node.

Optional sampling parameters are sent only when the user set them, and a
request the provider rejects *because of* one of them is retried once without
it: several current models refuse ``temperature`` outright, and OpenAI's newer
models refuse ``max_tokens`` in favour of ``max_completion_tokens``. Nothing
else is retried here; a chat call is not idempotent at the provider and the
workflow engine owns retries.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from provider_sdk import ProviderError, ProviderJsonClient, RetryCategory

from .errors import ProviderCallFailed

#: Anthropic requires ``max_tokens``. Adaptive thinking on current models
#: counts against it, so a small default would cut answers off mid-thought.
ANTHROPIC_DEFAULT_MAX_TOKENS = 16_000
ANTHROPIC_OFFICIAL_HOST = "api.anthropic.com"
#: Models whose safety classifiers can decline a request; on Anthropic's own
#: API the server-side ``fallbacks: "default"`` option re-runs a declined
#: request on Anthropic's recommended fallback model inside the same call.
ANTHROPIC_SERVER_SIDE_FALLBACK_MODELS = frozenset({"claude-opus-5", "claude-fable-5-1"})
ANTHROPIC_SERVER_SIDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_LISTED_MODELS = 500


@dataclass(frozen=True)
class RemoteModel:
    id: str
    label: str | None = None

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label or self.id}


@dataclass(frozen=True)
class ChatResult:
    text: str
    #: The model the provider reports as having served the request, which can
    #: differ from the one requested (an alias, or a refusal fallback).
    model: str
    finish_reason: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def usage(self) -> dict[str, Any]:
        return {
            "served_model": self.model,
            "finish_reason": self.finish_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            **self.extra,
        }


class ChatClient(ABC):
    @abstractmethod
    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult: ...

    @abstractmethod
    async def list_models(self) -> list[RemoteModel]: ...


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _failed(exc: ProviderError) -> ProviderCallFailed:
    return ProviderCallFailed(str(exc), code=exc.code or "PROVIDER_CALL_FAILED")


def _rejected_parameter(exc: ProviderError, *names: str) -> bool:
    if exc.category is not RetryCategory.INVALID_REQUEST:
        return False
    message = str(exc).lower()
    return any(name in message for name in names)


class OpenAICompatibleChatClient(ChatClient):
    def __init__(self, client: ProviderJsonClient):
        self._client = client

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": ([{"role": "system", "content": system}] if system else []) + list(messages),
        }
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if temperature is not None:
            body["temperature"] = float(temperature)
        try:
            data = await self._post(body)
        except ProviderError as exc:
            retry = dict(body)
            if "max_tokens" in retry and _rejected_parameter(exc, "max_completion_tokens"):
                retry["max_completion_tokens"] = retry.pop("max_tokens")
            elif "temperature" in retry and _rejected_parameter(exc, "temperature"):
                retry.pop("temperature")
            else:
                raise _failed(exc) from exc
            try:
                data = await self._post(retry)
            except ProviderError as retry_exc:
                raise _failed(retry_exc) from retry_exc
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        text = _openai_text(message.get("content"))
        finish_reason = first.get("finish_reason")
        if not text.strip():
            refusal = message.get("refusal")
            if refusal or finish_reason == "content_filter":
                raise ProviderCallFailed(
                    f"the model declined the request: {str(refusal or finish_reason)[:300]}",
                    code="MODEL_REFUSED",
                )
            raise ProviderCallFailed("the model returned no text", code="EMPTY_RESPONSE")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ChatResult(
            text=text,
            model=str(data.get("model") or model),
            finish_reason=str(finish_reason) if finish_reason else None,
            input_tokens=_int_or_none(usage.get("prompt_tokens")),
            output_tokens=_int_or_none(usage.get("completion_tokens")),
        )

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._client.request("POST", "/chat/completions", json_body=body, submitted=True)

    async def list_models(self) -> list[RemoteModel]:
        try:
            data = await self._client.request("GET", "/models")
        except ProviderError as exc:
            raise _failed(exc) from exc
        entries = data.get("data") if isinstance(data.get("data"), list) else []
        models: list[RemoteModel] = []
        for entry in entries[:MAX_LISTED_MODELS]:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].strip():
                label = entry.get("name") if isinstance(entry.get("name"), str) else None
                models.append(RemoteModel(entry["id"].strip(), label))
        return models


def _openai_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}:
                parts.append(str(part.get("text") or ""))
        return "".join(parts)
    return ""


class AnthropicChatClient(ChatClient):
    """The Anthropic Messages API (``POST /v1/messages``) over raw HTTP.

    Raw HTTP rather than the Anthropic SDK because every connection must pass
    the platform's connect-time egress fence, which lives in this package's
    transport; the request and response shapes follow Anthropic's API
    reference (``x-api-key``, ``anthropic-version: 2023-06-01``).
    """

    def __init__(self, client: ProviderJsonClient, *, base_url: str):
        self._client = client
        normalized = base_url.rstrip("/")
        # Accept a base URL entered with or without the /v1 version segment.
        self._prefix = "" if normalized.endswith("/v1") else "/v1"
        host = normalized.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        self._official_host = host == ANTHROPIC_OFFICIAL_HOST

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": int(max_tokens) if max_tokens else ANTHROPIC_DEFAULT_MAX_TOKENS,
            "messages": list(messages),
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = float(temperature)
        headers: dict[str, str] = {}
        if self._official_host and model in ANTHROPIC_SERVER_SIDE_FALLBACK_MODELS:
            headers["anthropic-beta"] = ANTHROPIC_SERVER_SIDE_FALLBACK_BETA
            body["fallbacks"] = "default"
        try:
            data = await self._post(body, headers)
        except ProviderError as exc:
            retry = dict(body)
            retry_headers = dict(headers)
            if "temperature" in retry and _rejected_parameter(exc, "temperature"):
                retry.pop("temperature")
            elif "fallbacks" in retry and _rejected_parameter(exc, "fallbacks", "anthropic-beta"):
                retry.pop("fallbacks")
                retry_headers.pop("anthropic-beta", None)
            else:
                raise _failed(exc) from exc
            try:
                data = await self._post(retry, retry_headers)
            except ProviderError as retry_exc:
                raise _failed(retry_exc) from retry_exc
        stop_reason = data.get("stop_reason")
        if stop_reason == "refusal":
            details = data.get("stop_details") if isinstance(data.get("stop_details"), dict) else {}
            category = details.get("category")
            suffix = f" (category: {str(category)[:60]})" if category else ""
            raise ProviderCallFailed(f"the model declined the request{suffix}", code="MODEL_REFUSED")
        blocks = data.get("content") if isinstance(data.get("content"), list) else []
        text = "".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text.strip():
            raise ProviderCallFailed(
                f"the model returned no text (stop reason: {str(stop_reason)[:40]})",
                code="EMPTY_RESPONSE",
            )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ChatResult(
            text=text,
            model=str(data.get("model") or model),
            finish_reason=str(stop_reason) if stop_reason else None,
            input_tokens=_int_or_none(usage.get("input_tokens")),
            output_tokens=_int_or_none(usage.get("output_tokens")),
        )

    async def _post(self, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        return await self._client.request(
            "POST", f"{self._prefix}/messages", json_body=body, headers=headers, submitted=True
        )

    async def list_models(self) -> list[RemoteModel]:
        models: list[RemoteModel] = []
        after_id: str | None = None
        for _page in range(5):
            query = {"limit": "100"}
            if after_id:
                query["after_id"] = after_id
            try:
                data = await self._client.request("GET", f"{self._prefix}/models", query=query)
            except ProviderError as exc:
                raise _failed(exc) from exc
            entries = data.get("data") if isinstance(data.get("data"), list) else []
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].strip():
                    label = entry.get("display_name") if isinstance(entry.get("display_name"), str) else None
                    models.append(RemoteModel(entry["id"].strip(), label))
            last_id = data.get("last_id")
            if not data.get("has_more") or not isinstance(last_id, str) or len(models) >= MAX_LISTED_MODELS:
                break
            after_id = last_id
        return models[:MAX_LISTED_MODELS]


class MockChatClient(ChatClient):
    """Development stand-in: a marked, deterministic echo. It never touches a network."""

    def __init__(self, models: tuple[str, ...] = ("mock-chat",)):
        self._models = models

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        latest = next(
            (message.get("content", "") for message in reversed(messages) if message.get("role") == "user"),
            "",
        )
        body = " ".join(str(latest).split())
        text = f"[mock {model}] {body}" if body else f"[mock {model}] (no input)"
        if max_tokens:
            text = text[: max(16, int(max_tokens) * 4)]
        return ChatResult(
            text=text,
            model=model,
            finish_reason="stop",
            input_tokens=max(1, (len(body) + len(system or "")) // 4),
            output_tokens=max(1, len(text) // 4),
            extra={"mock": True},
        )

    async def list_models(self) -> list[RemoteModel]:
        return [RemoteModel(model) for model in self._models]


__all__ = [
    "ANTHROPIC_DEFAULT_MAX_TOKENS",
    "AnthropicChatClient",
    "ChatClient",
    "ChatResult",
    "MockChatClient",
    "OpenAICompatibleChatClient",
    "RemoteModel",
]
