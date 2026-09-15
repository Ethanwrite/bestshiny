// Run from the repo root: node --test apps/web/canvas/graph.test.mjs
import assert from "node:assert/strict";
import test from "node:test";

import {
  CLIPBOARD_TYPE,
  GRAPH_SCHEMA,
  GraphStore,
  HISTORY_LIMIT,
  ID_PATTERN,
  MAX_NODES,
  STARTER_NOTE,
  STARTER_PROMPT,
  checkConnection,
  connectionCandidates,
  defaultData,
  downstreamOf,
  effectiveData,
  effectiveValue,
  indexCatalog,
  normalizeDocument,
  paramVisible,
  parseClipboardText,
  runScope,
  starterGraph,
  topologicalOrder,
  upstreamOf,
  validateForRun,
  visibleParams,
} from "./graph.js";

/* A copy of the server catalogue (core/workflows/workflow_core/catalog.py,
   canvas-nodes-v1) - the only place outside the server that spells out node
   types, and only so these rules can be tested offline. */
const SOURCE = [{ value: "platform", label: "BestShiny credits" }, { value: "connection", label: "My API connection" }];
const port = (key, label, dataType, extra = {}) => ({
  key, label, data_type: dataType, required: false, multiple: false, max_connections: 1, ...extra,
});
const param = (key, label, kind, extra = {}) => ({
  key, label, kind, default: null, required: false, options: [], min: null, max: null, step: null,
  max_length: null, capability: null, visible_when: {}, inline: false, placeholder: "", help: "", ...extra,
});
const ON_PLATFORM = { source: ["platform"] };
const ON_CONNECTION = { source: ["connection"] };

export const CATALOG = {
  version: "canvas-nodes-v1",
  graph_schema: "canvas-graph-v1",
  data_types: {
    text: { label: "Text", color: "info" },
    image: { label: "Image", color: "brand" },
    video: { label: "Video", color: "violet" },
  },
  categories: [{ id: "input", label: "Inputs" }, { id: "generate", label: "Generate" }, { id: "utility", label: "Utility" }],
  nodes: [
    {
      type: "text", title: "Text", category: "input", description: "Write text.", icon: "text", executable: true,
      inputs: [], outputs: [port("text", "Text", "text")],
      params: [param("text", "Text", "textarea", { default: "", max_length: 20000, inline: true })],
    },
    {
      type: "image_input", title: "Image", category: "input", description: "Upload an image.", icon: "image", executable: true,
      inputs: [], outputs: [port("image", "Image", "image")],
      params: [param("asset_id", "Image", "asset", { required: true, inline: true })],
    },
    {
      type: "llm", title: "LLM", category: "generate", description: "Run a language model.", icon: "sparkles", executable: true,
      inputs: [port("prompt", "Prompt", "text"), port("context", "Context", "text", { multiple: true, max_connections: 8 })],
      outputs: [port("text", "Text", "text")],
      params: [
        param("connection_id", "Connection", "connection", { required: true, capability: "chat" }),
        param("model", "Model", "model", { required: true, capability: "chat" }),
        param("instruction", "Instruction", "textarea", { default: "", inline: true }),
        param("system_prompt", "System prompt", "textarea", { default: "" }),
        param("temperature", "Temperature", "number", { min: 0, max: 2, step: 0.1 }),
        param("max_tokens", "Max output tokens", "integer", { min: 1, max: 128000 }),
      ],
    },
    {
      type: "image_generation", title: "Image generation", category: "generate", description: "Generate an image.", icon: "image-sparkles", executable: true,
      inputs: [port("prompt", "Prompt", "text"), port("reference", "References", "image", { multiple: true, max_connections: 6 })],
      outputs: [port("image", "Image", "image")],
      params: [
        param("source", "Run on", "select", { default: "platform", options: SOURCE }),
        param("image_tier", "Quality", "select", { default: "shiny", options: [{ value: "shiny", label: "Shiny" }], visible_when: ON_PLATFORM }),
        param("connection_id", "Connection", "connection", { capability: "image", visible_when: ON_CONNECTION }),
        param("model", "Model", "model", { capability: "image", visible_when: ON_CONNECTION }),
        param("prompt", "Prompt", "textarea", { default: "", inline: true }),
        param("aspect_ratio", "Aspect ratio", "select", { default: "1:1", options: [{ value: "1:1", label: "1:1" }] }),
      ],
    },
    {
      type: "video_generation", title: "Video generation", category: "generate", description: "Generate a video.", icon: "film", executable: true,
      inputs: [
        port("prompt", "Prompt", "text"),
        port("first_frame", "First frame", "image"),
        port("last_frame", "Last frame", "image"),
        port("reference", "References", "image", { multiple: true, max_connections: 4 }),
      ],
      outputs: [port("video", "Video", "video")],
      params: [
        param("source", "Run on", "select", { default: "platform", options: SOURCE }),
        param("platform_model", "Model", "platform_video_model", { default: "", visible_when: ON_PLATFORM }),
        param("connection_id", "Connection", "connection", { capability: "video", visible_when: ON_CONNECTION }),
        param("model", "Model", "model", { capability: "video", visible_when: ON_CONNECTION }),
        param("prompt", "Prompt", "textarea", { default: "", inline: true }),
        param("duration", "Duration (s)", "integer", { default: 5, min: 1, max: 30 }),
        param("aspect_ratio", "Aspect ratio", "select", { default: "16:9", options: [{ value: "16:9", label: "16:9" }] }),
        param("resolution", "Resolution", "select", { default: "720p", options: [{ value: "720p", label: "720p" }] }),
        param("negative_prompt", "Negative prompt", "textarea", { default: "" }),
        param("generate_audio", "Generate audio", "boolean", { default: true, visible_when: ON_CONNECTION }),
      ],
    },
    {
      type: "note", title: "Note", category: "utility", description: "A sticky note.", icon: "note", executable: false,
      inputs: [], outputs: [], params: [param("text", "Note", "textarea", { default: "", inline: true })],
    },
  ],
};

const newStore = (document = null) => new GraphStore({ catalog: CATALOG, document });
const edge = (source, sourcePort, target, targetPort) => ({
  source, source_port: sourcePort, target, target_port: targetPort,
});

/* ------------------------------------------------------------------ catalogue helpers */

test("the catalogue index resolves types and fills defaults the way the server stores them", () => {
  const index = indexCatalog(CATALOG);
  assert.equal(indexCatalog(index), index, "an index passes through");
  assert.equal(index.get("nope"), null);
  const video = index.get("video_generation");
  assert.deepEqual(defaultData(video), {
    source: "platform", platform_model: "", prompt: "", duration: 5, aspect_ratio: "16:9",
    resolution: "720p", negative_prompt: "", generate_audio: true,
  });
  const source = video.params.find((item) => item.key === "source");
  const duration = video.params.find((item) => item.key === "duration");
  const connection = video.params.find((item) => item.key === "connection_id");
  assert.equal(effectiveValue(source, { source: "" }), "platform", "an emptied select is its default");
  assert.equal(effectiveValue(duration, { duration: "" }), null, "an emptied number is unset");
  assert.equal(effectiveValue(connection, {}), null);
  assert.equal(effectiveData(video, {}).generate_audio, true);
});

test("visible_when is re-evaluated against the effective data", () => {
  const video = indexCatalog(CATALOG).get("video_generation");
  const keys = (data) => visibleParams(video, data).map((item) => item.key);
  assert.ok(keys({}).includes("platform_model"));
  assert.ok(!keys({}).includes("connection_id"));
  assert.ok(keys({ source: "connection" }).includes("connection_id"));
  assert.ok(keys({ source: "connection" }).includes("generate_audio"));
  assert.ok(!keys({ source: "connection" }).includes("platform_model"));
  assert.equal(paramVisible({ visible_when: {} }, {}), true);
  assert.equal(paramVisible({ visible_when: { source: "connection" } }, { source: "connection" }), false,
    "a malformed rule never shows the field");
});

/* ------------------------------------------------------------------ nodes */

test("addNode applies catalogue defaults, keeps given data and hands out valid ids", () => {
  const store = newStore();
  const node = store.addNode("video_generation", { x: 10.4, y: 20.6 }, { prompt: "rain" }, { label: "  Hero   shot " });
  assert.match(node.id, ID_PATTERN);
  assert.deepEqual(node.position, { x: 10, y: 21 });
  assert.equal(node.data.prompt, "rain");
  assert.equal(node.data.duration, 5);
  assert.equal(node.label, "Hero shot");
  assert.equal(store.addNode("does_not_exist", { x: 0, y: 0 }), null);
  assert.match(store.lastError, /Unknown node type/);
});

test("a canvas stops at the server's node limit", () => {
  const store = newStore();
  for (let index = 0; index < MAX_NODES; index += 1) store.addNode("note", { x: index, y: 0 });
  assert.equal(store.nodes.length, MAX_NODES);
  assert.equal(store.addNode("note", { x: 0, y: 0 }), null);
  assert.match(store.lastError, /at most 300/);
});

test("updateNodeData patches immutably and a typing burst is one undo step", () => {
  const store = newStore();
  const node = store.addNode("text", { x: 0, y: 0 });
  const before = store.getNode(node.id);
  store.updateNodeData(node.id, { text: "a" }, { coalesce: true });
  store.updateNodeData(node.id, { text: "ab" }, { coalesce: true });
  store.updateNodeData(node.id, { text: "abc" }, { coalesce: true });
  assert.equal(store.getNode(node.id).data.text, "abc");
  assert.equal(before.data.text, "", "the old object is untouched");
  assert.throws(() => { store.getNode(node.id).data.text = "x"; }, TypeError, "node data is frozen");
  assert.ok(store.undo());
  assert.equal(store.getNode(node.id).data.text, "", "the burst undoes at once");
  assert.ok(store.undo());
  assert.equal(store.getNode(node.id), null, "and the add before it is its own step");
});

test("an unchanged patch is not a change", () => {
  const store = newStore();
  const node = store.addNode("text", { x: 0, y: 0 }, { text: "same" });
  const events = [];
  store.subscribe((event) => events.push(event.kind));
  store.updateNodeData(node.id, { text: "same" });
  assert.deepEqual(events, []);
});

test("renameNode trims, caps the length and clears with an empty title", () => {
  const store = newStore();
  const node = store.addNode("text", { x: 0, y: 0 });
  store.renameNode(node.id, `  ${"x".repeat(200)}  `);
  assert.equal(store.getNode(node.id).label.length, 120);
  store.renameNode(node.id, "   ");
  assert.equal("label" in store.getNode(node.id), false);
});

test("a drag inside a batch moves many nodes and undoes as one step", () => {
  const store = newStore();
  const a = store.addNode("text", { x: 0, y: 0 });
  const b = store.addNode("text", { x: 100, y: 0 });
  const kinds = [];
  store.subscribe((event) => kinds.push(event.kind));
  store.beginBatch();
  for (let step = 1; step <= 10; step += 1) {
    store.setPositions([{ id: a.id, x: step * 5, y: step }, { id: b.id, x: 100 + step * 5, y: step }]);
  }
  store.endBatch();
  assert.deepEqual(store.getNode(a.id).position, { x: 50, y: 10 });
  assert.equal(kinds.filter((kind) => kind === "move").length, 10, "every frame is rendered");
  assert.ok(store.undo());
  assert.deepEqual(store.getNode(a.id).position, { x: 0, y: 0 });
  assert.deepEqual(store.getNode(b.id).position, { x: 100, y: 0 });
  assert.ok(store.undo(), "the adds are still there to undo");
  store.moveNodes([b.id], 0, 0);
  assert.equal(store.canRedo, true, "a no-op move does not clear redo");
});

test("a move keeps the node's data object, so cards know nothing but the position changed", () => {
  const store = newStore();
  const node = store.addNode("text", { x: 0, y: 0 }, { text: "keep" });
  const before = store.getNode(node.id);
  store.moveNodes([node.id], 10, 0);
  const after = store.getNode(node.id);
  assert.notEqual(after, before);
  assert.equal(after.data, before.data);
  assert.notEqual(after.position, before.position);
  store.updateNodeData(node.id, { text: "changed" });
  assert.equal(store.getNode(node.id).position, after.position);
});

test("a cancelled batch puts the document back and leaves no history", () => {
  const store = newStore();
  const text = store.addNode("text", { x: 0, y: 0 });
  const llm = store.addNode("llm", { x: 300, y: 0 });
  const wired = store.connect(edge(text.id, "text", llm.id, "prompt")).edge;
  const undoDepth = store._undo.length;
  const kinds = [];
  store.subscribe((event) => kinds.push([event.kind, event.origin]));
  store.beginBatch();
  store.removeEdges([wired.id]);
  store.moveNodes([text.id], 50, 50);
  store.cancelBatch();
  assert.deepEqual(store.edges.map((item) => item.id), [wired.id]);
  assert.deepEqual(store.getNode(text.id).position, { x: 0, y: 0 });
  assert.equal(store._undo.length, undoDepth);
  assert.deepEqual(kinds.at(-1), ["replace", "cancel"]);
});

test("removeNodes takes every attached edge with it", () => {
  const store = newStore();
  const text = store.addNode("text", { x: 0, y: 0 });
  const llm = store.addNode("llm", { x: 300, y: 0 });
  const video = store.addNode("video_generation", { x: 600, y: 0 });
  store.connect(edge(text.id, "text", llm.id, "prompt"));
  store.connect(edge(llm.id, "text", video.id, "prompt"));
  let removal = null;
  store.subscribe((event) => { if (event.kind === "remove") removal = event; });
  const result = store.removeNodes([llm.id]);
  assert.equal(store.edges.length, 0);
  assert.equal(result.edgeIds.length, 2);
  assert.deepEqual(new Set(removal.touchedNodeIds), new Set([text.id, video.id]));
  store.undo();
  assert.equal(store.edges.length, 2);
  assert.ok(store.getNode(llm.id));
});

test("removeEdges removes only the named edges", () => {
  const store = newStore();
  const text = store.addNode("text", { x: 0, y: 0 });
  const llm = store.addNode("llm", { x: 300, y: 0 });
  const first = store.connect(edge(text.id, "text", llm.id, "prompt")).edge;
  const second = store.connect(edge(text.id, "text", llm.id, "context")).edge;
  assert.deepEqual(store.removeEdges([first.id, "missing"]), [first.id]);
  assert.deepEqual(store.edges.map((item) => item.id), [second.id]);
});

/* ------------------------------------------------------------------ edge rules */

test("an edge must run from an output to an input", () => {
  const store = newStore();
  const text = store.addNode("text", { x: 0, y: 0 });
  const llm = store.addNode("llm", { x: 300, y: 0 });
  assert.equal(store.canConnect(edge(llm.id, "prompt", text.id, "text")).reason, "unknown-output");
  assert.equal(store.canConnect(edge(text.id, "text", llm.id, "text")).reason, "unknown-input");
  assert.equal(store.canConnect(edge(text.id, "text", "gone", "prompt")).reason, "missing-node");
  assert.equal(store.canConnect(edge(text.id, "text", llm.id, "prompt")).ok, true);
});

test("an edge joins ports of the same data type only", () => {
  const store = newStore();
  const image = store.addNode("image_input", { x: 0, y: 0 });
  const video = store.addNode("video_generation", { x: 300, y: 0 });
  const verdict = store.canConnect(edge(image.id, "image", video.id, "prompt"));
  assert.equal(verdict.ok, false);
  assert.equal(verdict.reason, "type");
  assert.equal(verdict.message, "Image cannot connect to a text input.");
  assert.equal(store.canConnect(edge(image.id, "image", video.id, "first_frame")).ok, true);
});

test("a node never connects to itself and a pair connects once", () => {
  const store = newStore();
  const llm = store.addNode("llm", { x: 0, y: 0 });
  const other = store.addNode("llm", { x: 300, y: 0 });
  assert.equal(store.canConnect(edge(llm.id, "text", llm.id, "context")).reason, "self");
  assert.equal(store.connect(edge(llm.id, "text", other.id, "context")).ok, true);
  assert.equal(store.canConnect(edge(llm.id, "text", other.id, "context")).reason, "duplicate");
});

test("a single-capacity input swaps its edge for the new one", () => {
  const store = newStore();
  const one = store.addNode("text", { x: 0, y: 0 });
  const two = store.addNode("text", { x: 0, y: 200 });
  const video = store.addNode("video_generation", { x: 300, y: 0 });
  const first = store.connect(edge(one.id, "text", video.id, "prompt"));
  const check = store.canConnect(edge(two.id, "text", video.id, "prompt"));
  assert.equal(check.ok, true);
  assert.equal(check.replaces, first.edge.id);
  const second = store.connect(edge(two.id, "text", video.id, "prompt"));
  assert.equal(second.replaced, first.edge.id);
  assert.deepEqual(store.incoming(video.id, "prompt").map((item) => item.source), [two.id]);
  store.undo();
  assert.deepEqual(store.incoming(video.id, "prompt").map((item) => item.source), [one.id], "undo restores the old edge");
});

test("a multiple input holds up to max_connections", () => {
  const store = newStore();
  const video = store.addNode("video_generation", { x: 600, y: 0 });
  const images = Array.from({ length: 5 }, (_, index) => store.addNode("image_input", { x: 0, y: index * 100 }));
  for (const image of images.slice(0, 4)) {
    assert.equal(store.connect(edge(image.id, "image", video.id, "reference")).ok, true);
  }
  const refused = store.connect(edge(images[4].id, "image", video.id, "reference"));
  assert.equal(refused.ok, false);
  assert.equal(refused.reason, "capacity");
  assert.equal(refused.message, "References accepts at most 4 connections.");
  const llm = store.addNode("llm", { x: 300, y: 0 });
  const texts = Array.from({ length: 9 }, (_, index) => store.addNode("text", { x: 0, y: 600 + index * 100 }));
  const results = texts.map((text) => store.connect(edge(text.id, "text", llm.id, "context")).ok);
  assert.deepEqual(results, [true, true, true, true, true, true, true, true, false]);
});

test("no connection may close a cycle, directly or through other nodes", () => {
  const store = newStore();
  const a = store.addNode("llm", { x: 0, y: 0 });
  const b = store.addNode("llm", { x: 300, y: 0 });
  const c = store.addNode("llm", { x: 600, y: 0 });
  store.connect(edge(a.id, "text", b.id, "prompt"));
  store.connect(edge(b.id, "text", c.id, "prompt"));
  assert.equal(store.canConnect(edge(b.id, "text", a.id, "context")).reason, "cycle");
  assert.equal(store.canConnect(edge(c.id, "text", a.id, "prompt")).reason, "cycle");
  assert.equal(store.canConnect(edge(a.id, "text", c.id, "context")).ok, true, "a diamond is not a cycle");
  assert.notEqual(topologicalOrder(store.doc), null);
});

test("replacing an input's edge is judged without the edge it replaces", () => {
  const store = newStore();
  const a = store.addNode("llm", { x: 0, y: 0 });
  const b = store.addNode("llm", { x: 300, y: 0 });
  store.connect(edge(a.id, "text", b.id, "prompt"));
  const check = checkConnection(store.doc, CATALOG, edge(a.id, "text", b.id, "prompt"));
  assert.equal(check.reason, "duplicate");
});

test("connectionCandidates grades every opposite port for a drag", () => {
  const store = newStore();
  const text = store.addNode("text", { x: 0, y: 0 });
  const image = store.addNode("image_input", { x: 0, y: 200 });
  const video = store.addNode("video_generation", { x: 300, y: 0 });
  const fromText = connectionCandidates(store.doc, CATALOG, { nodeId: text.id, port: "text", direction: "out" });
  assert.equal(fromText.get(`${video.id}:prompt`).ok, true);
  assert.equal(fromText.get(`${video.id}:first_frame`).reason, "type");
  const intoFirstFrame = connectionCandidates(store.doc, CATALOG, { nodeId: video.id, port: "first_frame", direction: "in" });
  assert.equal(intoFirstFrame.get(`${image.id}:image`).ok, true);
  assert.equal(intoFirstFrame.get(`${text.id}:text`).ok, false);
});

/* ------------------------------------------------------------------ history */

test("undo and redo walk the history, and a new change clears redo", () => {
  const store = newStore();
  const a = store.addNode("text", { x: 0, y: 0 });
  store.addNode("text", { x: 100, y: 0 });
  assert.equal(store.nodes.length, 2);
  store.undo();
  assert.equal(store.nodes.length, 1);
  assert.equal(store.canRedo, true);
  store.redo();
  assert.equal(store.nodes.length, 2);
  store.undo();
  store.updateNodeData(a.id, { text: "fork" });
  assert.equal(store.canRedo, false);
  assert.equal(store.redo(), false);
});

test("history keeps the newest hundred steps", () => {
  const store = newStore();
  const node = store.addNode("text", { x: 0, y: 0 });
  for (let index = 0; index < HISTORY_LIMIT + 20; index += 1) store.moveNodes([node.id], 1, 0);
  let steps = 0;
  while (store.undo()) steps += 1;
  assert.equal(steps, HISTORY_LIMIT);
});

test("the viewport is saved state but never an undo step", () => {
  const store = newStore();
  const events = [];
  store.subscribe((event) => events.push(event));
  store.setViewport({ x: 12, y: -4, zoom: 9 });
  assert.deepEqual(store.viewport, { x: 12, y: -4, zoom: 2.5 });
  store.setViewport({ zoom: 0.01 });
  assert.equal(store.viewport.zoom, 0.1);
  assert.equal(store.canUndo, false);
  assert.ok(events.every((event) => event.kind === "viewport" && event.docChanged === false));
  assert.equal(store.toJSON().viewport.zoom, 0.1);
});

test("load replaces the document, starts history over and is not a user change", () => {
  const store = newStore();
  store.addNode("text", { x: 0, y: 0 });
  const events = [];
  store.subscribe((event) => events.push(event));
  store.load({ schema: GRAPH_SCHEMA, nodes: [], edges: [], viewport: { x: 1, y: 2, zoom: 1 } });
  assert.equal(store.nodes.length, 0);
  assert.equal(store.canUndo, false);
  assert.deepEqual(events.map((event) => [event.kind, event.origin, event.docChanged]), [["replace", "load", false]]);
});

/* ------------------------------------------------------------------ clipboard */

test("copy takes the selected nodes and only the edges between them", () => {
  const store = newStore();
  const text = store.addNode("text", { x: 0, y: 0 }, { text: "hello" });
  const llm = store.addNode("llm", { x: 300, y: 0 });
  const video = store.addNode("video_generation", { x: 600, y: 0 });
  store.connect(edge(text.id, "text", llm.id, "prompt"));
  store.connect(edge(llm.id, "text", video.id, "prompt"));
  const payload = store.copy([text.id, llm.id]);
  assert.equal(payload.type, CLIPBOARD_TYPE);
  assert.equal(payload.nodes.length, 2);
  assert.equal(payload.edges.length, 1);
  assert.equal(store.copy([]), null);
  assert.deepEqual(parseClipboardText(JSON.stringify(payload)), payload);
  assert.equal(parseClipboardText("plain words"), null);
});

test("paste adds fresh ids, keeps internal edges and lands where it is asked", () => {
  const store = newStore();
  const text = store.addNode("text", { x: 100, y: 100 }, { text: "hello" });
  const llm = store.addNode("llm", { x: 400, y: 150 });
  store.connect(edge(text.id, "text", llm.id, "prompt"));
  const payload = store.copy([text.id, llm.id]);
  const ids = store.paste(payload, { at: { x: 1000, y: 2000 } });
  assert.equal(ids.length, 2);
  assert.ok(ids.every((id) => ID_PATTERN.test(id) && id !== text.id && id !== llm.id));
  const pasted = ids.map((id) => store.getNode(id));
  assert.deepEqual(pasted.map((node) => node.position), [{ x: 1000, y: 2000 }, { x: 1300, y: 2050 }]);
  assert.equal(pasted[0].data.text, "hello");
  assert.equal(store.edges.length, 2);
  const internal = store.edges.find((item) => item.source === ids[0]);
  assert.equal(internal.target, ids[1]);
  assert.ok(store.undo());
  assert.equal(store.nodes.length, 2, "a paste is one undo step");
});

test("paste offsets by default, skips unknown types and duplicate reuses it", () => {
  const store = newStore();
  const text = store.addNode("text", { x: 0, y: 0 });
  const payload = store.copy([text.id]);
  payload.nodes.push({ id: "alien", type: "teleporter", position: { x: 5, y: 5 }, data: {} });
  const ids = store.paste(payload);
  assert.equal(ids.length, 1);
  assert.deepEqual(store.getNode(ids[0]).position, { x: 40, y: 40 });
  const twins = store.duplicate([text.id], { x: 20, y: 30 });
  assert.deepEqual(store.getNode(twins[0]).position, { x: 20, y: 30 });
  assert.deepEqual(store.paste({ type: "something-else", nodes: [] }), []);
});

/* ------------------------------------------------------------------ run rules */

test("run validation names every missing required, visible parameter", () => {
  const store = newStore();
  const image = store.addNode("image_input", { x: 0, y: 0 });
  const llm = store.addNode("llm", { x: 300, y: 0 }, { instruction: "Summarise" });
  const issues = validateForRun(store.doc, CATALOG);
  assert.deepEqual(
    issues.map((issue) => [issue.node_id, issue.param, issue.message]),
    [
      [image.id, "asset_id", "Image is required"],
      [llm.id, "connection_id", "Connection is required"],
      [llm.id, "model", "Model is required"],
    ],
  );
  store.updateNodeData(image.id, { asset_id: "asset-1" });
  store.updateNodeData(llm.id, { connection_id: "conn-1", model: "  " });
  assert.deepEqual(validateForRun(store.doc, CATALOG).map((issue) => issue.param), ["model"], "blank text is missing");
});

test("an LLM needs an instruction or a connected prompt", () => {
  const store = newStore();
  const llm = store.addNode("llm", { x: 300, y: 0 }, { connection_id: "c", model: "m" });
  assert.deepEqual(validateForRun(store.doc, CATALOG).map((issue) => issue.message),
    ["Write an instruction or connect text to Prompt"]);
  const text = store.addNode("text", { x: 0, y: 0 }, { text: "hi" });
  store.connect(edge(text.id, "text", llm.id, "prompt"));
  assert.deepEqual(validateForRun(store.doc, CATALOG), []);
});

test("a generation node needs a prompt, and a connection source needs its connection and model", () => {
  const store = newStore();
  const imageGen = store.addNode("image_generation", { x: 0, y: 0 });
  assert.deepEqual(validateForRun(store.doc, CATALOG).map((issue) => issue.param), ["prompt"]);
  store.updateNodeData(imageGen.id, { prompt: "a fox" });
  assert.deepEqual(validateForRun(store.doc, CATALOG), [], "the platform source needs nothing else");
  store.updateNodeData(imageGen.id, { source: "connection" });
  assert.deepEqual(validateForRun(store.doc, CATALOG).map((issue) => [issue.param, issue.message]),
    [["connection_id", "Connection is required"], ["model", "Model is required"]]);
  store.updateNodeData(imageGen.id, { connection_id: "c1", model: "m1" });
  assert.deepEqual(validateForRun(store.doc, CATALOG), []);
});

test("a last frame needs a first frame", () => {
  const store = newStore();
  const last = store.addNode("image_input", { x: 0, y: 0 }, { asset_id: "a1" });
  const video = store.addNode("video_generation", { x: 300, y: 0 }, { prompt: "go" });
  store.connect(edge(last.id, "image", video.id, "last_frame"));
  assert.deepEqual(validateForRun(store.doc, CATALOG).map((issue) => [issue.node_id, issue.port]), [[video.id, "last_frame"]]);
  const first = store.addNode("image_input", { x: 0, y: 200 }, { asset_id: "a2" });
  store.connect(edge(first.id, "image", video.id, "first_frame"));
  assert.deepEqual(validateForRun(store.doc, CATALOG), []);
});

test("notes never run and never fail validation", () => {
  const store = newStore();
  store.addNode("note", { x: 0, y: 0 });
  assert.deepEqual(validateForRun(store.doc, CATALOG), []);
  assert.deepEqual(runScope(store.doc, CATALOG).scope, []);
});

test("running some nodes validates them and what feeds them, nothing downstream", () => {
  const store = newStore();
  const text = store.addNode("text", { x: 0, y: 0 }, { text: "hi" });
  const brokenUpstream = store.addNode("llm", { x: 300, y: 0 });
  const video = store.addNode("video_generation", { x: 600, y: 0 });
  const downstream = store.addNode("llm", { x: 900, y: 0 });
  const unrelated = store.addNode("image_generation", { x: 0, y: 400 });
  store.connect(edge(text.id, "text", brokenUpstream.id, "prompt"));
  store.connect(edge(brokenUpstream.id, "text", video.id, "prompt"));
  const scope = runScope(store.doc, CATALOG, [video.id]);
  assert.deepEqual(scope.targets, [video.id]);
  assert.deepEqual(scope.scope, [text.id, brokenUpstream.id, video.id]);
  const nodes = new Set(validateForRun(store.doc, CATALOG, [video.id]).map((issue) => issue.node_id));
  assert.deepEqual(nodes, new Set([brokenUpstream.id]));
  assert.ok(!nodes.has(downstream.id) && !nodes.has(unrelated.id));
  assert.deepEqual([...upstreamOf(store.doc, [video.id])].sort(), [text.id, brokenUpstream.id, video.id].sort());
  assert.deepEqual([...downstreamOf(store.doc, [text.id])].sort(), [text.id, brokenUpstream.id, video.id].sort());
});

/* ------------------------------------------------------------------ documents */

test("a loaded document keeps valid parts and drops dangling or malformed ones", () => {
  const doc = normalizeDocument({
    nodes: [
      { id: "a", type: "text", position: { x: 1, y: 2 }, data: { text: "x" } },
      { id: "a", type: "text", position: { x: 9, y: 9 } },
      { id: "bad id!", type: "text" },
      { id: "b", type: "mystery", position: { x: "nope" } },
    ],
    edges: [
      { id: "e1", source: "a", source_port: "text", target: "b", target_port: "in" },
      { id: "e2", source: "a", source_port: "text", target: "ghost", target_port: "prompt" },
    ],
    viewport: { x: 5, zoom: 40 },
  });
  assert.deepEqual(doc.nodes.map((node) => node.id), ["a", "b"]);
  assert.deepEqual(doc.nodes[1].position, { x: 0, y: 0 });
  assert.deepEqual(doc.edges.map((item) => item.id), ["e1"]);
  assert.deepEqual(doc.viewport, { x: 5, y: 0, zoom: 2.5 });
});

test("toJSON is the canvas-graph-v1 wire document", () => {
  const store = newStore();
  const text = store.addNode("text", { x: 0, y: 0 }, { text: "hi" }, { label: "Idea" });
  const video = store.addNode("video_generation", { x: 300, y: 0 });
  store.connect(edge(text.id, "text", video.id, "prompt"));
  const json = store.toJSON();
  assert.equal(json.schema, "canvas-graph-v1");
  assert.deepEqual(Object.keys(json.nodes[0]), ["id", "type", "position", "data", "label"]);
  assert.deepEqual(Object.keys(json.edges[0]), ["id", "source", "source_port", "target", "target_port"]);
  const again = newStore(JSON.parse(JSON.stringify(json)));
  assert.deepEqual(again.toJSON(), json, "a saved document loads back identically");
});

test("the starter canvas wires its prompt into a platform video node and is ready to run", () => {
  const json = starterGraph(CATALOG);
  const types = json.nodes.map((node) => node.type).sort();
  assert.deepEqual(types, ["note", "text", "video_generation"]);
  const text = json.nodes.find((node) => node.type === "text");
  const video = json.nodes.find((node) => node.type === "video_generation");
  const note = json.nodes.find((node) => node.type === "note");
  assert.equal(text.data.text, STARTER_PROMPT);
  assert.equal(note.data.text, STARTER_NOTE);
  assert.equal(video.data.source, "platform");
  assert.deepEqual(json.edges.map((item) => [item.source, item.source_port, item.target, item.target_port]),
    [[text.id, "text", video.id, "prompt"]]);
  const store = newStore(json);
  assert.deepEqual(validateForRun(store.doc, CATALOG), []);
});

test("events tell the editor what changed", () => {
  const store = newStore();
  const seen = [];
  store.subscribe((event) => seen.push([event.kind, event.docChanged]));
  const text = store.addNode("text", { x: 0, y: 0 });
  const video = store.addNode("video_generation", { x: 300, y: 0 });
  store.updateNodeData(text.id, { text: "a" });
  store.renameNode(text.id, "Prompt");
  store.moveNodes([text.id], 5, 5);
  store.connect(edge(text.id, "text", video.id, "prompt"));
  store.setViewport({ x: 3 });
  store.removeNodes([video.id]);
  store.undo();
  assert.deepEqual(seen, [
    ["add", true], ["add", true], ["data", true], ["label", true], ["move", true],
    ["edges", true], ["viewport", false], ["remove", true], ["replace", true],
  ]);
});
