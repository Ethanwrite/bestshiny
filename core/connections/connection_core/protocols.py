"""The wire protocols a workspace connection can speak.

A protocol is a wire format, not a vendor: ``openai_compatible`` covers every
API that implements ``POST /chat/completions``. Video and image generation are
offered only where this repository already has an adapter written against the
vendor's own API reference (OpenRouter, Volcengine Ark, Alibaba DashScope), so
a connection reuses that reviewed request builder with the workspace's key
instead of a second, unreviewed one.

Suggested model ids are the provider model ids the platform's own registry
(``config/model-registry/defaults.json``) already runs, or the ids the
Anthropic API reference names; they are suggestions for a picker, never a
restriction - a user may type any model their account can use.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from provider_sdk import USER_CONNECTION_PROVIDER


class ConnectionCapability(StrEnum):
    CHAT = "chat"
    IMAGE = "image"
    VIDEO = "video"


class AuthStyle(StrEnum):
    BEARER = "bearer"
    X_API_KEY = "x-api-key"


@dataclass(frozen=True)
class ProtocolPreset:
    id: str
    label: str
    base_url: str
    suggested_models: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectionProtocol:
    id: str
    label: str
    description: str
    capabilities: frozenset[str]
    default_base_url: str
    auth: AuthStyle
    base_url_hint: str = ""
    presets: tuple[ProtocolPreset, ...] = ()
    suggested_models: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Whether ``GET .../models`` lists the account's models.
    lists_models: bool = False
    #: Whether that listing requires the key, so a successful listing proves
    #: the key. OpenRouter's model list is public: listing proves nothing.
    listing_checks_key: bool = True
    #: Network-free, for demos and browser QA; refused outside development.
    development_only: bool = False
    default_headers: Mapping[str, str] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "capabilities": sorted(self.capabilities),
            "default_base_url": self.default_base_url,
            "base_url_hint": self.base_url_hint,
            "presets": [
                {
                    "id": preset.id,
                    "label": preset.label,
                    "base_url": preset.base_url,
                    "suggested_models": {key: list(value) for key, value in preset.suggested_models.items()},
                }
                for preset in self.presets
            ],
            "suggested_models": {key: list(value) for key, value in self.suggested_models.items()},
            "lists_models": self.lists_models,
            "listing_checks_key": self.listing_checks_key,
            "development_only": self.development_only,
        }


PROTOCOLS: dict[str, ConnectionProtocol] = {
    protocol.id: protocol
    for protocol in (
        ConnectionProtocol(
            id="openai_compatible",
            label="OpenAI-compatible",
            description=(
                "Any chat API that implements POST /chat/completions with a Bearer key: OpenAI, "
                "DeepSeek, Moonshot, Zhipu GLM, SiliconFlow, Google Gemini's OpenAI endpoint, "
                "xAI, Groq, Mistral, Qwen and Doubao compatible modes, or your own gateway."
            ),
            capabilities=frozenset({ConnectionCapability.CHAT.value}),
            default_base_url="https://api.openai.com/v1",
            auth=AuthStyle.BEARER,
            base_url_hint="The URL that /chat/completions is appended to, usually ending in /v1.",
            presets=(
                ProtocolPreset("openai", "OpenAI", "https://api.openai.com/v1"),
                ProtocolPreset(
                    "deepseek",
                    "DeepSeek",
                    "https://api.deepseek.com",
                    {ConnectionCapability.CHAT.value: ("deepseek-v4-flash",)},
                ),
                ProtocolPreset("moonshot", "Moonshot (Kimi)", "https://api.moonshot.cn/v1"),
                ProtocolPreset("zhipu", "Zhipu GLM", "https://open.bigmodel.cn/api/paas/v4"),
                ProtocolPreset("siliconflow", "SiliconFlow", "https://api.siliconflow.cn/v1"),
                ProtocolPreset(
                    "qwen",
                    "Qwen (DashScope compatible mode)",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    {ConnectionCapability.CHAT.value: ("qwen3.8-max",)},
                ),
                ProtocolPreset(
                    "doubao",
                    "Doubao (Volcengine Ark)",
                    "https://ark.cn-beijing.volces.com/api/v3",
                    {ConnectionCapability.CHAT.value: ("doubao-seed-2-0-lite-260428", "glm-5.2")},
                ),
                ProtocolPreset(
                    "gemini",
                    "Google Gemini (OpenAI endpoint)",
                    "https://generativelanguage.googleapis.com/v1beta/openai",
                ),
                ProtocolPreset("xai", "xAI", "https://api.x.ai/v1"),
                ProtocolPreset("groq", "Groq", "https://api.groq.com/openai/v1"),
                ProtocolPreset("mistral", "Mistral", "https://api.mistral.ai/v1"),
            ),
            lists_models=True,
        ),
        ConnectionProtocol(
            id="anthropic",
            label="Anthropic",
            description="Claude models through the Anthropic Messages API with your own API key.",
            capabilities=frozenset({ConnectionCapability.CHAT.value}),
            default_base_url="https://api.anthropic.com",
            auth=AuthStyle.X_API_KEY,
            base_url_hint="https://api.anthropic.com, or a gateway that serves /v1/messages.",
            suggested_models={
                ConnectionCapability.CHAT.value: (
                    "claude-opus-5",
                    "claude-sonnet-5",
                    "claude-haiku-4-5",
                    "claude-fable-5-1",
                )
            },
            lists_models=True,
            default_headers={"anthropic-version": "2023-06-01"},
        ),
        ConnectionProtocol(
            id="openrouter",
            label="OpenRouter",
            description=(
                "One key for chat, image and video models routed by OpenRouter - including Kling, "
                "Veo, Wan and Grok video."
            ),
            capabilities=frozenset(
                {
                    ConnectionCapability.CHAT.value,
                    ConnectionCapability.IMAGE.value,
                    ConnectionCapability.VIDEO.value,
                }
            ),
            default_base_url="https://openrouter.ai/api/v1",
            auth=AuthStyle.BEARER,
            listing_checks_key=False,
            suggested_models={
                ConnectionCapability.CHAT.value: (
                    "anthropic/claude-opus-5",
                    "anthropic/claude-sonnet-5",
                    "openai/gpt-5.6-sol",
                ),
                ConnectionCapability.IMAGE.value: ("openai/gpt-image-2",),
                ConnectionCapability.VIDEO.value: (
                    "kwaivgi/kling-v3.0-pro",
                    "kwaivgi/kling-v3.0-std",
                    "google/veo-3.1",
                    "google/veo-3.1-fast",
                    "google/veo-3.1-lite",
                    "alibaba/wan-3.0",
                    "x-ai/grok-imagine-video",
                ),
            },
            lists_models=True,
        ),
        ConnectionProtocol(
            id="volcengine_ark",
            label="Volcengine Ark",
            description="Doubao chat, Seedream images and Seedance video with your Ark API key.",
            capabilities=frozenset(
                {
                    ConnectionCapability.CHAT.value,
                    ConnectionCapability.IMAGE.value,
                    ConnectionCapability.VIDEO.value,
                }
            ),
            default_base_url="https://ark.cn-beijing.volces.com/api/v3",
            auth=AuthStyle.BEARER,
            base_url_hint="Your Ark region's API base, ending in /api/v3.",
            suggested_models={
                ConnectionCapability.CHAT.value: ("doubao-seed-2-0-lite-260428", "glm-5.2"),
                ConnectionCapability.IMAGE.value: ("doubao-seedream-5-0-260128",),
                ConnectionCapability.VIDEO.value: ("doubao-seedance-2-5-260628",),
            },
        ),
        ConnectionProtocol(
            id="alibaba_dashscope",
            label="Alibaba Cloud Model Studio",
            description="Qwen chat and Wan video (DashScope) with your Model Studio API key.",
            capabilities=frozenset({ConnectionCapability.CHAT.value, ConnectionCapability.VIDEO.value}),
            default_base_url="https://dashscope.aliyuncs.com",
            auth=AuthStyle.BEARER,
            base_url_hint="The DashScope host for your region, without a path.",
            presets=(
                ProtocolPreset("china", "China (Beijing)", "https://dashscope.aliyuncs.com"),
                ProtocolPreset(
                    "international", "International (Singapore)", "https://dashscope-intl.aliyuncs.com"
                ),
            ),
            suggested_models={
                ConnectionCapability.CHAT.value: ("qwen3.8-max",),
                ConnectionCapability.VIDEO.value: (
                    "wan2.7-t2v-2026-06-12",
                    "wan2.7-i2v-2026-04-25",
                    "wan2.7-r2v-2026-06-12",
                ),
            },
        ),
        ConnectionProtocol(
            id="mock",
            label="Mock (development)",
            description=(
                "A network-free stand-in that answers chat with a marked echo and generates "
                "placeholder images and videos. Available in development only."
            ),
            capabilities=frozenset(
                {
                    ConnectionCapability.CHAT.value,
                    ConnectionCapability.IMAGE.value,
                    ConnectionCapability.VIDEO.value,
                }
            ),
            default_base_url="https://mock.invalid",
            auth=AuthStyle.BEARER,
            suggested_models={
                ConnectionCapability.CHAT.value: ("mock-chat",),
                ConnectionCapability.IMAGE.value: ("mock-image",),
                ConnectionCapability.VIDEO.value: ("mock-video",),
            },
            lists_models=True,
            development_only=True,
        ),
    )
}


def protocol_for(protocol_id: str) -> ConnectionProtocol | None:
    return PROTOCOLS.get((protocol_id or "").strip())


__all__ = [
    "AuthStyle",
    "ConnectionCapability",
    "ConnectionProtocol",
    "PROTOCOLS",
    "ProtocolPreset",
    "USER_CONNECTION_PROVIDER",
    "protocol_for",
]
