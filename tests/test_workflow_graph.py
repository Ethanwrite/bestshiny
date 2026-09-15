"""The canvas document rules: what saves, what runs, and what counts as unchanged."""

from __future__ import annotations

import copy

import pytest
from workflow_core import (
    GRAPH_SCHEMA,
    NODE_TYPES,
    WorkflowGraphInvalid,
    catalog_view,
    empty_graph,
    fingerprints,
    parse_graph,
    run_issues,
)


def node(node_id: str, node_type: str, **data):  # type: ignore[no-untyped-def]
    return {"id": node_id, "type": node_type, "position": {"x": 0, "y": 0}, "data": data}


def edge(edge_id: str, source: str, source_port: str, target: str, target_port: str) -> dict[str, str]:
    return {
        "id": edge_id,
        "source": source,
        "source_port": source_port,
        "target": target,
        "target_port": target_port,
    }


def document(nodes, edges=()):  # type: ignore[no-untyped-def]
    return {"schema": GRAPH_SCHEMA, "nodes": list(nodes), "edges": list(edges)}


PIPELINE = document(
    [
        node("t1", "text", text="A lighthouse at dusk"),
        node("l1", "llm", connection_id="c1", model="m1", instruction="Make it cinematic"),
        node("i1", "image_generation", prompt=""),
        node("v1", "video_generation", source="connection", connection_id="c2", model="kling"),
        node("n1", "note", text="remember the rain"),
    ],
    [
        edge("e1", "t1", "text", "l1", "prompt"),
        edge("e2", "l1", "text", "i1", "prompt"),
        edge("e3", "l1", "text", "v1", "prompt"),
        edge("e4", "i1", "image", "v1", "first_frame"),
    ],
)


def _issue_codes(document_value) -> list[str]:  # type: ignore[no-untyped-def]
    with pytest.raises(WorkflowGraphInvalid) as invalid:
        parse_graph(document_value)
    return [issue.code for issue in invalid.value.issues]


def test_catalog_is_complete_and_serializable():
    view = catalog_view()
    types = {item["type"] for item in view["nodes"]}
    assert types == {"text", "image_input", "llm", "image_generation", "video_generation", "note"}
    assert set(view["data_types"]) == {"text", "image", "video"}
    for spec in NODE_TYPES.values():
        keys = [param.key for param in spec.params]
        assert len(keys) == len(set(keys)), spec.type
        for param in spec.params:
            for condition in param.visible_when:
                assert condition in keys, (spec.type, param.key, condition)


def test_a_valid_pipeline_round_trips_with_defaults_filled():
    graph = parse_graph(PIPELINE)
    saved = graph.to_json()
    assert saved["schema"] == GRAPH_SCHEMA
    video = next(item for item in saved["nodes"] if item["id"] == "v1")
    assert video["data"]["duration"] == 5 and video["data"]["aspect_ratio"] == "16:9"
    assert parse_graph(saved).to_json() == saved
    assert graph.topological_order().index("t1") < graph.topological_order().index("v1")
    assert graph.upstream(["v1"]) == {"v1", "l1", "i1", "t1"}
    assert parse_graph(empty_graph()).nodes == {}


@pytest.mark.parametrize(
    ("edges", "code"),
    [
        ([edge("e1", "t1", "text", "i1", "reference")], "TYPE_MISMATCH"),
        ([edge("e1", "i1", "image", "v1", "prompt")], "TYPE_MISMATCH"),
        (
            [edge("e1", "t1", "text", "v1", "prompt"), edge("e2", "t2", "text", "v1", "prompt")],
            "TOO_MANY_CONNECTIONS",
        ),
    ],
)
def test_edges_join_matching_types_within_their_capacity(edges, code):  # type: ignore[no-untyped-def]
    nodes = [
        node("t1", "text"),
        node("t2", "text"),
        node("i1", "image_generation"),
        node("v1", "video_generation"),
    ]
    assert code in _issue_codes(document(nodes, edges))


def test_multiple_inputs_accept_up_to_their_limit():
    nodes = [node(f"img{index}", "image_input") for index in range(7)] + [node("g", "image_generation")]
    edges = [edge(f"e{index}", f"img{index}", "image", "g", "reference") for index in range(6)]
    parse_graph(document(nodes, edges))
    edges.append(edge("e6", "img6", "image", "g", "reference"))
    assert "TOO_MANY_CONNECTIONS" in _issue_codes(document(nodes, edges))


def test_cycles_self_loops_duplicates_and_dangling_edges_are_refused():
    nodes = [node("a", "llm"), node("b", "llm")]
    assert "CYCLE" in _issue_codes(
        document(nodes, [edge("e1", "a", "text", "b", "prompt"), edge("e2", "b", "text", "a", "prompt")])
    )
    with pytest.raises(WorkflowGraphInvalid, match="itself"):
        parse_graph(document(nodes, [edge("e1", "a", "text", "a", "context")]))
    with pytest.raises(WorkflowGraphInvalid, match="twice"):
        parse_graph(
            document(
                nodes,
                [edge("e1", "a", "text", "b", "context"), edge("e2", "a", "text", "b", "context")],
            )
        )
    with pytest.raises(WorkflowGraphInvalid, match="missing node"):
        parse_graph(document(nodes, [edge("e1", "a", "text", "zzz", "prompt")]))
    with pytest.raises(WorkflowGraphInvalid, match="unknown output"):
        parse_graph(document(nodes, [edge("e1", "a", "video", "b", "prompt")]))
    assert "DUPLICATE_NODE" in _issue_codes(document([node("a", "text"), node("a", "text")]))


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"duration": 0}, "at least"),
        ({"duration": 2.5}, "whole number"),
        ({"duration": True}, "whole number"),
        ({"aspect_ratio": "7:3"}, "one of"),
        ({"source": "somewhere"}, "one of"),
        ({"prompt": 5}, "must be text"),
        ({"prompt": "x" * 30_001}, "longer than"),
        ({"generate_audio": "yes"}, "true or false"),
    ],
)
def test_parameters_that_are_set_must_be_valid(data, fragment):  # type: ignore[no-untyped-def]
    with pytest.raises(WorkflowGraphInvalid, match=fragment):
        parse_graph(document([node("v", "video_generation", **data)]))


def test_emptied_optional_fields_mean_unset():
    graph = parse_graph(document([node("l", "llm", temperature="", max_tokens="", connection_id="")]))
    data = graph.nodes["l"].data
    assert data["temperature"] is None and data["max_tokens"] is None and data["connection_id"] is None


def test_document_limits_and_schema():
    with pytest.raises(WorkflowGraphInvalid, match="unsupported workflow schema"):
        parse_graph({"schema": "canvas-graph-v0", "nodes": [], "edges": []})
    with pytest.raises(WorkflowGraphInvalid, match="at most"):
        parse_graph(document([node(f"n{index}", "note") for index in range(301)]))
    with pytest.raises(WorkflowGraphInvalid, match="invalid id"):
        parse_graph(document([node("has space", "note")]))
    with pytest.raises(WorkflowGraphInvalid, match="unknown node type"):
        parse_graph(document([node("x", "teleporter")]))


def test_a_half_built_canvas_saves_but_does_not_run():
    graph = parse_graph(document([node("l", "llm"), node("v", "video_generation", source="connection")]))
    issues = run_issues(graph, ["l", "v"])
    by_node = {(issue.node_id, issue.param or issue.port) for issue in issues}
    assert ("l", "connection_id") in by_node and ("l", "model") in by_node
    assert ("l", "instruction") in by_node, "an LLM needs an instruction or a connected prompt"
    assert ("v", "prompt") in by_node
    assert ("v", "connection_id") in by_node and ("v", "model") in by_node


def test_run_rules_follow_what_is_connected_and_what_is_visible():
    graph = parse_graph(PIPELINE)
    assert run_issues(graph, list(graph.nodes)) == []
    # image_generation on the platform needs no connection even though the
    # connection fields exist on the node type.
    platform_image = parse_graph(document([node("i", "image_generation", prompt="a cat")]))
    assert run_issues(platform_image, ["i"]) == []
    last_without_first = parse_graph(
        document(
            [node("img", "image_input", asset_id="a1"), node("v", "video_generation", prompt="go")],
            [edge("e", "img", "image", "v", "last_frame")],
        )
    )
    assert [issue.port for issue in run_issues(last_without_first, ["v"])] == ["last_frame"]
    image_without_asset = parse_graph(document([node("img", "image_input")]))
    assert [issue.param for issue in run_issues(image_without_asset, ["img"])] == ["asset_id"]


def test_fingerprints_change_with_what_a_node_is_asked_and_nothing_else():
    base = fingerprints(parse_graph(PIPELINE))

    moved = copy.deepcopy(PIPELINE)
    for item in moved["nodes"]:
        item["position"] = {"x": 500, "y": 900}
    moved["nodes"][4]["data"]["text"] = "a different note"
    executable = ("t1", "l1", "i1", "v1")
    moved_prints = fingerprints(parse_graph(moved))
    assert {key: moved_prints[key] for key in executable} == {key: base[key] for key in executable}, (
        "layout and notes never invalidate a result"
    )

    edited = copy.deepcopy(PIPELINE)
    edited["nodes"][1]["data"]["instruction"] = "Make it noir"
    changed = fingerprints(parse_graph(edited))
    assert changed["t1"] == base["t1"]
    assert changed["l1"] != base["l1"]
    assert changed["i1"] != base["i1"] and changed["v1"] != base["v1"], "a change flows downstream"

    hidden = copy.deepcopy(PIPELINE)
    hidden["nodes"][3]["data"]["platform_model"] = "seedance:doubao-seedance-2-5-260628"
    hidden["nodes"][3]["data"]["image_tier"] = "shiniest"
    assert fingerprints(parse_graph(hidden))["v1"] == base["v1"], (
        "parameters the node does not use do not count"
    )
