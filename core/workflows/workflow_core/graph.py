"""The canvas document: parsing, structural validation, run validation, fingerprints.

Two levels of validation, deliberately different:

- ``parse_graph`` is what a save must pass. The document is well formed,
  every node type and port exists, every edge joins an output to an input of
  the same data type, no input exceeds its connection limit, the graph has no
  cycle, and every parameter that *is* set has the right type and range. A
  half-built canvas - a video node with no prompt yet - saves fine.
- ``run_issues`` is what a run must pass: within the nodes the run will
  execute, every required parameter and input is present and each node's own
  rules hold. It returns every problem at once, keyed by node, so the editor
  can mark all of them.

A node's fingerprint hashes its type, its visible parameters and, in port
order, the fingerprints of whatever feeds it. Two runs whose fingerprints for a
node agree asked that node for the same thing, which is what lets an unchanged
node reuse an earlier result instead of paying for it again.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .catalog import GRAPH_SCHEMA, NODE_TYPES, NodeType, ParamKind, ParamSpec

MAX_NODES = 300
MAX_EDGES = 1_000
MAX_DOCUMENT_BYTES = 1_000_000
MAX_LABEL = 120
MAX_REFERENCE_VALUE = 200
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_COORDINATE_LIMIT = 1_000_000


@dataclass(frozen=True)
class GraphIssue:
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    port: str | None = None
    param: str | None = None
    code: str = "INVALID"

    def view(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "node_id": self.node_id,
            "edge_id": self.edge_id,
            "port": self.port,
            "param": self.param,
            "code": self.code,
        }


class WorkflowGraphInvalid(ValueError):
    reason_code = "WORKFLOW_INVALID"

    def __init__(self, issues: list[GraphIssue], message: str | None = None):
        self.issues = issues
        first = issues[0].message if issues else "the workflow is invalid"
        super().__init__(message or (first if len(issues) == 1 else f"{first} (and {len(issues) - 1} more)"))

    def as_detail(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "reason_code": self.reason_code,
            "errors": [issue.view() for issue in self.issues],
        }


@dataclass(frozen=True)
class GraphNode:
    id: str
    type: str
    x: float
    y: float
    data: dict[str, Any]
    label: str | None = None

    @property
    def spec(self) -> NodeType:
        return NODE_TYPES[self.type]

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "position": {"x": self.x, "y": self.y},
            "data": self.data,
        }
        if self.label:
            document["label"] = self.label
        return document


@dataclass(frozen=True)
class GraphEdge:
    id: str
    source: str
    source_port: str
    target: str
    target_port: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "source_port": self.source_port,
            "target": self.target,
            "target_port": self.target_port,
        }


@dataclass(frozen=True)
class WorkflowGraph:
    nodes: dict[str, GraphNode]
    edges: tuple[GraphEdge, ...]
    viewport: dict[str, float] = field(default_factory=lambda: {"x": 0.0, "y": 0.0, "zoom": 1.0})

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": GRAPH_SCHEMA,
            "nodes": [node.to_json() for node in self.nodes.values()],
            "edges": [edge.to_json() for edge in self.edges],
            "viewport": dict(self.viewport),
        }

    def incoming(self, node_id: str) -> list[GraphEdge]:
        """Edges into a node, in the node type's port order, then document order."""

        node = self.nodes[node_id]
        order = {port.key: index for index, port in enumerate(node.spec.inputs)}
        edges = [edge for edge in self.edges if edge.target == node_id]
        positions = {id(edge): index for index, edge in enumerate(self.edges)}
        return sorted(edges, key=lambda edge: (order.get(edge.target_port, 99), positions[id(edge)]))

    def inputs_on(self, node_id: str, port: str) -> list[GraphEdge]:
        return [edge for edge in self.incoming(node_id) if edge.target_port == port]

    def topological_order(self) -> list[str]:
        indegree = {node_id: 0 for node_id in self.nodes}
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in self.nodes}
        for edge in self.edges:
            indegree[edge.target] += 1
            outgoing[edge.source].append(edge.target)
        queue = deque(node_id for node_id in self.nodes if indegree[node_id] == 0)
        order: list[str] = []
        while queue:
            current = queue.popleft()
            order.append(current)
            for target in outgoing[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if len(order) != len(self.nodes):
            raise WorkflowGraphInvalid([GraphIssue("the workflow contains a cycle", code="CYCLE")])
        return order

    def upstream(self, node_ids: Iterable[str]) -> set[str]:
        """The given nodes and everything that feeds them, transitively."""

        result: set[str] = set()
        stack = [node_id for node_id in node_ids if node_id in self.nodes]
        while stack:
            current = stack.pop()
            if current in result:
                continue
            result.add(current)
            stack.extend(edge.source for edge in self.edges if edge.target == current)
        return result

    def downstream(self, node_ids: Iterable[str]) -> set[str]:
        result: set[str] = set()
        stack = [node_id for node_id in node_ids if node_id in self.nodes]
        while stack:
            current = stack.pop()
            if current in result:
                continue
            result.add(current)
            stack.extend(edge.target for edge in self.edges if edge.source == current)
        return result


def empty_graph() -> dict[str, Any]:
    return WorkflowGraph(nodes={}, edges=()).to_json()


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def normalize_param(spec: ParamSpec, value: Any) -> tuple[Any, str | None]:
    """Return the stored value for one parameter, or an error message."""

    kind = spec.kind
    if value is None:
        return spec.default, None
    if value == "" and kind in {ParamKind.SELECT, ParamKind.BOOLEAN}:
        return spec.default, None
    if value == "" and kind in {
        ParamKind.NUMBER,
        ParamKind.INTEGER,
        ParamKind.CONNECTION,
        ParamKind.MODEL,
        ParamKind.ASSET,
    }:
        # An emptied field is "not set", which for these kinds is no value.
        return None, None
    if kind in {ParamKind.TEXT, ParamKind.TEXTAREA}:
        if not isinstance(value, str):
            return None, f"{spec.label} must be text"
        if spec.max_length is not None and len(value) > spec.max_length:
            return None, f"{spec.label} is longer than {spec.max_length} characters"
        return value, None
    if kind is ParamKind.NUMBER:
        if not _finite(value):
            return None, f"{spec.label} must be a number"
        number = float(value)
        if spec.minimum is not None and number < spec.minimum:
            return None, f"{spec.label} must be at least {spec.minimum:g}"
        if spec.maximum is not None and number > spec.maximum:
            return None, f"{spec.label} must be at most {spec.maximum:g}"
        return number, None
    if kind is ParamKind.INTEGER:
        if not _finite(value) or float(value) != int(value):
            return None, f"{spec.label} must be a whole number"
        integer = int(value)
        if spec.minimum is not None and integer < spec.minimum:
            return None, f"{spec.label} must be at least {spec.minimum:g}"
        if spec.maximum is not None and integer > spec.maximum:
            return None, f"{spec.label} must be at most {spec.maximum:g}"
        return integer, None
    if kind is ParamKind.SELECT:
        allowed = {option for option, _label in spec.options}
        if value not in allowed:
            return None, f"{spec.label} must be one of: {', '.join(sorted(allowed))}"
        return value, None
    if kind is ParamKind.BOOLEAN:
        if not isinstance(value, bool):
            return None, f"{spec.label} must be true or false"
        return value, None
    # Identifiers: connection, model, asset, platform video model.
    if not isinstance(value, str):
        return None, f"{spec.label} must be text"
    trimmed = value.strip()
    if len(trimmed) > MAX_REFERENCE_VALUE or any(ord(char) < 32 for char in trimmed):
        return None, f"{spec.label} is not a valid value"
    return trimmed, None


def parse_graph(document: Any) -> WorkflowGraph:
    issues: list[GraphIssue] = []
    if not isinstance(document, dict):
        raise WorkflowGraphInvalid([GraphIssue("the workflow document must be an object")])
    try:
        size = len(json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise WorkflowGraphInvalid([GraphIssue("the workflow document is not valid JSON")]) from exc
    if size > MAX_DOCUMENT_BYTES:
        raise WorkflowGraphInvalid([GraphIssue(f"the workflow is larger than {MAX_DOCUMENT_BYTES} bytes")])
    schema = document.get("schema", GRAPH_SCHEMA)
    if schema != GRAPH_SCHEMA:
        raise WorkflowGraphInvalid([GraphIssue(f"unsupported workflow schema: {str(schema)[:40]}")])
    raw_nodes = document.get("nodes", [])
    raw_edges = document.get("edges", [])
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise WorkflowGraphInvalid([GraphIssue("nodes and edges must be lists")])
    if len(raw_nodes) > MAX_NODES:
        raise WorkflowGraphInvalid([GraphIssue(f"a workflow can have at most {MAX_NODES} nodes")])
    if len(raw_edges) > MAX_EDGES:
        raise WorkflowGraphInvalid([GraphIssue(f"a workflow can have at most {MAX_EDGES} connections")])

    nodes: dict[str, GraphNode] = {}
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            issues.append(GraphIssue(f"node {index + 1} must be an object"))
            continue
        node_id = raw.get("id")
        if not isinstance(node_id, str) or not _IDENTIFIER.match(node_id):
            issues.append(GraphIssue(f"node {index + 1} has an invalid id"))
            continue
        if node_id in nodes:
            issues.append(GraphIssue("two nodes share an id", node_id=node_id, code="DUPLICATE_NODE"))
            continue
        node_type = raw.get("type")
        spec = NODE_TYPES.get(node_type) if isinstance(node_type, str) else None
        if spec is None:
            issues.append(GraphIssue(f"unknown node type: {str(node_type)[:40]}", node_id=node_id))
            continue
        position = raw.get("position") if isinstance(raw.get("position"), dict) else {}
        x, y = position.get("x", 0), position.get("y", 0)
        if not (_finite(x) and _finite(y)) or abs(x) > _COORDINATE_LIMIT or abs(y) > _COORDINATE_LIMIT:
            issues.append(GraphIssue("the node position is invalid", node_id=node_id))
            continue
        raw_data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        data: dict[str, Any] = {}
        for param in spec.params:
            value, error = normalize_param(param, raw_data.get(param.key))
            if error:
                issues.append(GraphIssue(error, node_id=node_id, param=param.key))
                continue
            data[param.key] = value
        label = raw.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > MAX_LABEL):
            issues.append(GraphIssue("the node title is invalid", node_id=node_id))
            label = None
        nodes[node_id] = GraphNode(
            id=node_id,
            type=spec.type,
            x=float(x),
            y=float(y),
            data=data,
            label=(label.strip() or None) if isinstance(label, str) else None,
        )

    edges: list[GraphEdge] = []
    edge_ids: set[str] = set()
    pairs: set[tuple[str, str, str, str]] = set()
    fan_in: dict[tuple[str, str], int] = {}
    for index, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            issues.append(GraphIssue(f"connection {index + 1} must be an object"))
            continue
        edge_id = raw.get("id")
        if not isinstance(edge_id, str) or not _IDENTIFIER.match(edge_id) or edge_id in edge_ids:
            issues.append(GraphIssue(f"connection {index + 1} has an invalid or repeated id"))
            continue
        source, target = raw.get("source"), raw.get("target")
        source_port, target_port = raw.get("source_port"), raw.get("target_port")
        if source not in nodes or target not in nodes:
            issues.append(GraphIssue("a connection points at a missing node", edge_id=edge_id))
            continue
        if source == target:
            issues.append(GraphIssue("a node cannot connect to itself", edge_id=edge_id, node_id=source))
            continue
        output = nodes[source].spec.output(source_port) if isinstance(source_port, str) else None
        input_port = nodes[target].spec.input(target_port) if isinstance(target_port, str) else None
        if output is None:
            issues.append(
                GraphIssue("a connection starts from an unknown output", edge_id=edge_id, node_id=source)
            )
            continue
        if input_port is None:
            issues.append(
                GraphIssue("a connection ends at an unknown input", edge_id=edge_id, node_id=target)
            )
            continue
        if output.data_type is not input_port.data_type:
            issues.append(
                GraphIssue(
                    f"{output.data_type.value} cannot connect to a {input_port.data_type.value} input",
                    edge_id=edge_id,
                    node_id=target,
                    port=input_port.key,
                    code="TYPE_MISMATCH",
                )
            )
            continue
        pair = (source, output.key, target, input_port.key)
        if pair in pairs:
            issues.append(GraphIssue("the same connection appears twice", edge_id=edge_id, node_id=target))
            continue
        limit = input_port.max_connections if input_port.multiple else 1
        count = fan_in.get((target, input_port.key), 0) + 1
        if count > limit:
            issues.append(
                GraphIssue(
                    f"{input_port.label} accepts at most {limit} connection{'s' if limit != 1 else ''}",
                    edge_id=edge_id,
                    node_id=target,
                    port=input_port.key,
                    code="TOO_MANY_CONNECTIONS",
                )
            )
            continue
        fan_in[(target, input_port.key)] = count
        pairs.add(pair)
        edge_ids.add(edge_id)
        edges.append(GraphEdge(edge_id, source, output.key, target, input_port.key))

    raw_viewport = document.get("viewport") if isinstance(document.get("viewport"), dict) else {}
    viewport = {"x": 0.0, "y": 0.0, "zoom": 1.0}
    for key in ("x", "y", "zoom"):
        value = raw_viewport.get(key)
        if _finite(value):
            viewport[key] = float(value)
    viewport["zoom"] = min(4.0, max(0.1, viewport["zoom"]))
    viewport["x"] = max(-_COORDINATE_LIMIT, min(_COORDINATE_LIMIT, viewport["x"]))
    viewport["y"] = max(-_COORDINATE_LIMIT, min(_COORDINATE_LIMIT, viewport["y"]))

    if issues:
        raise WorkflowGraphInvalid(issues)
    graph = WorkflowGraph(nodes=nodes, edges=tuple(edges), viewport=viewport)
    graph.topological_order()  # raises on a cycle
    return graph


def _text_set(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    return isinstance(value, str) and bool(value.strip())


def run_issues(graph: WorkflowGraph, node_ids: Iterable[str]) -> list[GraphIssue]:
    issues: list[GraphIssue] = []
    for node_id in node_ids:
        node = graph.nodes[node_id]
        spec = node.spec
        if not spec.executable:
            continue
        for param in spec.params:
            if not param.required or not param.visible(node.data):
                continue
            value = node.data.get(param.key)
            if value is None or (isinstance(value, str) and not value.strip()):
                issues.append(GraphIssue(f"{param.label} is required", node_id=node_id, param=param.key))
        for port in spec.inputs:
            if port.required and not graph.inputs_on(node_id, port.key):
                issues.append(
                    GraphIssue(f"connect something to {port.label}", node_id=node_id, port=port.key)
                )
        connected_prompt = bool(graph.inputs_on(node_id, "prompt")) if spec.input("prompt") else False
        if node.type == "llm":
            if not connected_prompt and not _text_set(node.data, "instruction"):
                issues.append(
                    GraphIssue(
                        "write an instruction or connect text to Prompt",
                        node_id=node_id,
                        param="instruction",
                    )
                )
        if node.type in {"image_generation", "video_generation"}:
            if not connected_prompt and not _text_set(node.data, "prompt"):
                issues.append(
                    GraphIssue("write a prompt or connect text to Prompt", node_id=node_id, param="prompt")
                )
            if node.data.get("source") == "connection":
                for key, label in (("connection_id", "Connection"), ("model", "Model")):
                    if not _text_set(node.data, key):
                        issues.append(GraphIssue(f"{label} is required", node_id=node_id, param=key))
        if node.type == "video_generation":
            if graph.inputs_on(node_id, "last_frame") and not graph.inputs_on(node_id, "first_frame"):
                issues.append(
                    GraphIssue("a last frame needs a first frame", node_id=node_id, port="last_frame")
                )
    return issues


def _visible_params(node: GraphNode) -> dict[str, Any]:
    return {param.key: node.data.get(param.key) for param in node.spec.params if param.visible(node.data)}


def fingerprints(graph: WorkflowGraph) -> dict[str, str]:
    result: dict[str, str] = {}
    for node_id in graph.topological_order():
        node = graph.nodes[node_id]
        payload = {
            "type": node.type,
            "params": _visible_params(node),
            "inputs": [
                [edge.target_port, result[edge.source], edge.source_port] for edge in graph.incoming(node_id)
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        result[node_id] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return result


def graph_hash(document: dict[str, Any]) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "GraphEdge",
    "GraphIssue",
    "GraphNode",
    "MAX_DOCUMENT_BYTES",
    "MAX_EDGES",
    "MAX_NODES",
    "WorkflowGraph",
    "WorkflowGraphInvalid",
    "empty_graph",
    "fingerprints",
    "graph_hash",
    "normalize_param",
    "parse_graph",
    "run_issues",
]
