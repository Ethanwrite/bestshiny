"""The canvas node catalogue: every node type, its typed ports and its parameters.

This is the single definition the editor renders from (``GET /v1/workflows/
node-types``) and the engine validates and executes against. A port carries
one of three data types - text, image, video - and an edge may only join an
output to an input of the same type. An input port takes one edge unless it is
``multiple``.

A generation node pays in one of two ways, chosen by its ``source`` parameter:
``platform`` runs on the platform's own models through admission and workspace
credits, exactly like the Create page; ``connection`` runs on one of the
workspace's own provider connections and is billed by that provider instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

CATALOG_VERSION = "canvas-nodes-v1"
GRAPH_SCHEMA = "canvas-graph-v1"


class DataType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"


class ParamKind(StrEnum):
    TEXT = "text"
    TEXTAREA = "textarea"
    NUMBER = "number"
    INTEGER = "integer"
    SELECT = "select"
    BOOLEAN = "boolean"
    #: A workspace connection id, filtered by ``capability``.
    CONNECTION = "connection"
    #: A model id on the selected connection, filtered by ``capability``.
    MODEL = "model"
    #: A media asset id in the workflow's project.
    ASSET = "asset"
    #: A platform video model, ``provider:model``, or "" for automatic.
    PLATFORM_VIDEO_MODEL = "platform_video_model"


@dataclass(frozen=True)
class PortSpec:
    key: str
    label: str
    data_type: DataType
    required: bool = False
    multiple: bool = False
    max_connections: int = 1

    def view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "data_type": self.data_type.value,
            "required": self.required,
            "multiple": self.multiple,
            "max_connections": self.max_connections,
        }


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    kind: ParamKind
    default: Any = None
    required: bool = False
    options: tuple[tuple[str, str], ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    max_length: int | None = None
    capability: str | None = None
    #: Shown only when another parameter holds one of these values.
    visible_when: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    #: The editor shows it on the node card, not only in the inspector.
    inline: bool = False
    placeholder: str = ""
    help: str = ""

    def visible(self, data: dict[str, Any]) -> bool:
        return all(data.get(key) in values for key, values in self.visible_when.items())

    def view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind.value,
            "default": self.default,
            "required": self.required,
            "options": [{"value": value, "label": label} for value, label in self.options],
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "max_length": self.max_length,
            "capability": self.capability,
            "visible_when": {key: list(values) for key, values in self.visible_when.items()},
            "inline": self.inline,
            "placeholder": self.placeholder,
            "help": self.help,
        }


@dataclass(frozen=True)
class NodeType:
    type: str
    title: str
    category: str
    description: str
    icon: str
    inputs: tuple[PortSpec, ...] = ()
    outputs: tuple[PortSpec, ...] = ()
    params: tuple[ParamSpec, ...] = ()
    executable: bool = True

    def input(self, key: str) -> PortSpec | None:
        return next((port for port in self.inputs if port.key == key), None)

    def output(self, key: str) -> PortSpec | None:
        return next((port for port in self.outputs if port.key == key), None)

    def param(self, key: str) -> ParamSpec | None:
        return next((param for param in self.params if param.key == key), None)

    def defaults(self) -> dict[str, Any]:
        return {param.key: param.default for param in self.params}

    def view(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "title": self.title,
            "category": self.category,
            "description": self.description,
            "icon": self.icon,
            "executable": self.executable,
            "inputs": [port.view() for port in self.inputs],
            "outputs": [port.view() for port in self.outputs],
            "params": [param.view() for param in self.params],
        }


_SOURCE_OPTIONS = (("platform", "BestShiny credits"), ("connection", "My API connection"))
_ON_PLATFORM = {"source": ("platform",)}
_ON_CONNECTION = {"source": ("connection",)}

_ASPECT_OPTIONS_VIDEO = (
    ("16:9", "16:9 landscape"),
    ("9:16", "9:16 portrait"),
    ("1:1", "1:1 square"),
    ("4:3", "4:3"),
    ("3:4", "3:4"),
    ("21:9", "21:9 cinema"),
)
_ASPECT_OPTIONS_IMAGE = (
    ("1:1", "1:1 square"),
    ("16:9", "16:9 landscape"),
    ("9:16", "9:16 portrait"),
    ("4:3", "4:3"),
    ("3:4", "3:4"),
)

NODE_TYPES: dict[str, NodeType] = {
    node.type: node
    for node in (
        NodeType(
            type="text",
            title="Text",
            category="input",
            description="Write a prompt, a script or any text and pass it downstream.",
            icon="text",
            outputs=(PortSpec("text", "Text", DataType.TEXT),),
            params=(
                ParamSpec(
                    "text",
                    "Text",
                    ParamKind.TEXTAREA,
                    default="",
                    max_length=20_000,
                    inline=True,
                    placeholder="Describe a scene, a character, a shot…",
                ),
            ),
        ),
        NodeType(
            type="image_input",
            title="Image",
            category="input",
            description="Upload an image to use as a reference or a first frame.",
            icon="image",
            outputs=(PortSpec("image", "Image", DataType.IMAGE),),
            params=(ParamSpec("asset_id", "Image", ParamKind.ASSET, required=True, inline=True),),
        ),
        NodeType(
            type="llm",
            title="LLM",
            category="generate",
            description="Run a language model on your own API connection.",
            icon="sparkles",
            inputs=(
                PortSpec("prompt", "Prompt", DataType.TEXT),
                PortSpec("context", "Context", DataType.TEXT, multiple=True, max_connections=8),
            ),
            outputs=(PortSpec("text", "Text", DataType.TEXT),),
            params=(
                ParamSpec(
                    "connection_id",
                    "Connection",
                    ParamKind.CONNECTION,
                    required=True,
                    capability="chat",
                ),
                ParamSpec("model", "Model", ParamKind.MODEL, required=True, capability="chat"),
                ParamSpec(
                    "instruction",
                    "Instruction",
                    ParamKind.TEXTAREA,
                    default="",
                    max_length=20_000,
                    inline=True,
                    placeholder="What should the model do? e.g. Rewrite this as a cinematic video prompt.",
                ),
                ParamSpec(
                    "system_prompt",
                    "System prompt",
                    ParamKind.TEXTAREA,
                    default="",
                    max_length=20_000,
                ),
                ParamSpec(
                    "temperature",
                    "Temperature",
                    ParamKind.NUMBER,
                    minimum=0,
                    maximum=2,
                    step=0.1,
                    help="Leave empty for the provider default. Some current models ignore or refuse it.",
                ),
                ParamSpec(
                    "max_tokens",
                    "Max output tokens",
                    ParamKind.INTEGER,
                    minimum=1,
                    maximum=128_000,
                    help="Leave empty for the provider default.",
                ),
            ),
        ),
        NodeType(
            type="image_generation",
            title="Image generation",
            category="generate",
            description="Generate an image from a prompt and optional reference images.",
            icon="image-sparkles",
            inputs=(
                PortSpec("prompt", "Prompt", DataType.TEXT),
                PortSpec("reference", "References", DataType.IMAGE, multiple=True, max_connections=6),
            ),
            outputs=(PortSpec("image", "Image", DataType.IMAGE),),
            params=(
                ParamSpec("source", "Run on", ParamKind.SELECT, default="platform", options=_SOURCE_OPTIONS),
                ParamSpec(
                    "image_tier",
                    "Quality",
                    ParamKind.SELECT,
                    default="shiny",
                    options=(("shiny", "Shiny"), ("shinier", "Shinier"), ("shiniest", "Shiniest")),
                    visible_when=_ON_PLATFORM,
                ),
                ParamSpec(
                    "connection_id",
                    "Connection",
                    ParamKind.CONNECTION,
                    capability="image",
                    visible_when=_ON_CONNECTION,
                ),
                ParamSpec(
                    "model",
                    "Model",
                    ParamKind.MODEL,
                    capability="image",
                    visible_when=_ON_CONNECTION,
                ),
                ParamSpec(
                    "prompt",
                    "Prompt",
                    ParamKind.TEXTAREA,
                    default="",
                    max_length=30_000,
                    inline=True,
                    placeholder="Used when no text is connected to the Prompt input.",
                ),
                ParamSpec(
                    "aspect_ratio",
                    "Aspect ratio",
                    ParamKind.SELECT,
                    default="1:1",
                    options=_ASPECT_OPTIONS_IMAGE,
                ),
            ),
        ),
        NodeType(
            type="video_generation",
            title="Video generation",
            category="generate",
            description="Generate a video from a prompt, with optional first/last frames or references.",
            icon="film",
            inputs=(
                PortSpec("prompt", "Prompt", DataType.TEXT),
                PortSpec("first_frame", "First frame", DataType.IMAGE),
                PortSpec("last_frame", "Last frame", DataType.IMAGE),
                PortSpec("reference", "References", DataType.IMAGE, multiple=True, max_connections=4),
            ),
            outputs=(PortSpec("video", "Video", DataType.VIDEO),),
            params=(
                ParamSpec("source", "Run on", ParamKind.SELECT, default="platform", options=_SOURCE_OPTIONS),
                ParamSpec(
                    "platform_model",
                    "Model",
                    ParamKind.PLATFORM_VIDEO_MODEL,
                    default="",
                    visible_when=_ON_PLATFORM,
                    help="Automatic picks the model your plan runs by default.",
                ),
                ParamSpec(
                    "connection_id",
                    "Connection",
                    ParamKind.CONNECTION,
                    capability="video",
                    visible_when=_ON_CONNECTION,
                ),
                ParamSpec(
                    "model",
                    "Model",
                    ParamKind.MODEL,
                    capability="video",
                    visible_when=_ON_CONNECTION,
                ),
                ParamSpec(
                    "prompt",
                    "Prompt",
                    ParamKind.TEXTAREA,
                    default="",
                    max_length=30_000,
                    inline=True,
                    placeholder="Used when no text is connected to the Prompt input.",
                ),
                ParamSpec("duration", "Duration (s)", ParamKind.INTEGER, default=5, minimum=1, maximum=30),
                ParamSpec(
                    "aspect_ratio",
                    "Aspect ratio",
                    ParamKind.SELECT,
                    default="16:9",
                    options=_ASPECT_OPTIONS_VIDEO,
                ),
                ParamSpec(
                    "resolution",
                    "Resolution",
                    ParamKind.SELECT,
                    default="720p",
                    options=(("480p", "480p"), ("720p", "720p"), ("1080p", "1080p")),
                ),
                ParamSpec(
                    "negative_prompt",
                    "Negative prompt",
                    ParamKind.TEXTAREA,
                    default="",
                    max_length=5_000,
                ),
                ParamSpec(
                    "generate_audio",
                    "Generate audio",
                    ParamKind.BOOLEAN,
                    default=True,
                    visible_when=_ON_CONNECTION,
                    help="Where the model supports it. Audio usually costs more.",
                ),
            ),
        ),
        NodeType(
            type="note",
            title="Note",
            category="utility",
            description="A sticky note for yourself or your team. Notes never run.",
            icon="note",
            executable=False,
            params=(
                ParamSpec("text", "Note", ParamKind.TEXTAREA, default="", max_length=5_000, inline=True),
            ),
        ),
    )
}

DATA_TYPES: dict[str, dict[str, str]] = {
    DataType.TEXT.value: {"label": "Text", "color": "info"},
    DataType.IMAGE.value: {"label": "Image", "color": "brand"},
    DataType.VIDEO.value: {"label": "Video", "color": "violet"},
}

CATEGORIES: tuple[dict[str, str], ...] = (
    {"id": "input", "label": "Inputs"},
    {"id": "generate", "label": "Generate"},
    {"id": "utility", "label": "Utility"},
)


def catalog_view() -> dict[str, Any]:
    return {
        "version": CATALOG_VERSION,
        "graph_schema": GRAPH_SCHEMA,
        "data_types": DATA_TYPES,
        "categories": list(CATEGORIES),
        "nodes": [node.view() for node in NODE_TYPES.values()],
    }


__all__ = [
    "CATALOG_VERSION",
    "CATEGORIES",
    "DATA_TYPES",
    "DataType",
    "GRAPH_SCHEMA",
    "NODE_TYPES",
    "NodeType",
    "ParamKind",
    "ParamSpec",
    "PortSpec",
    "catalog_view",
]
