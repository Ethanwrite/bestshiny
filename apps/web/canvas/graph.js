/**
 * The canvas document model: pure, no DOM, importable in Node.
 *
 * A canvas is a `canvas-graph-v1` document - nodes with typed ports, edges
 * joining an output to an input of the same data type, and a viewport. The
 * node catalogue (`GET /v1/workflows/node-types`) is the only definition of a
 * node type; nothing here names one, except `starterGraph`, which seeds a new
 * canvas from whatever the catalogue calls its text and video nodes.
 *
 * The edge rules and the run rules mirror the server's
 * (core/workflows/workflow_core/graph.py `parse_graph` / `run_issues`), so the
 * editor refuses a connection the server would refuse, and marks what a run
 * would reject before it is sent. The server stays the authority: its 422
 * errors are shown the same way.
 *
 * State is immutable by convention: every mutation builds new node objects and
 * new arrays. That makes a history snapshot two array references, and lets the
 * editor tell what changed by reference.
 */

export const GRAPH_SCHEMA = "canvas-graph-v1";
export const MAX_NODES = 300;
export const MAX_EDGES = 1000;
export const MAX_LABEL = 120;
export const HISTORY_LIMIT = 100;
export const ZOOM_MIN = 0.1;
export const ZOOM_MAX = 2.5;
export const ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
export const CLIPBOARD_TYPE = "bestshiny/canvas-clipboard";

/** Typing into one field within this long is one undo step. */
const COALESCE_MS = 1200;
const COORDINATE_LIMIT = 1_000_000;

const finite = (value) => typeof value === "number" && Number.isFinite(value);
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

/* ------------------------------------------------------------------ catalogue */

function normalizeSpec(spec) {
  return {
    ...spec,
    inputs: Array.isArray(spec.inputs) ? spec.inputs : [],
    outputs: Array.isArray(spec.outputs) ? spec.outputs : [],
    params: Array.isArray(spec.params) ? spec.params : [],
    executable: spec.executable !== false,
  };
}

/** Wrap the catalogue response in lookups. Accepts an index and returns it unchanged. */
export function indexCatalog(catalog) {
  if (catalog && typeof catalog.get === "function" && catalog.types instanceof Map) return catalog;
  const list = Array.isArray(catalog?.nodes) ? catalog.nodes : [];
  const types = new Map();
  for (const spec of list) {
    if (spec && typeof spec.type === "string") types.set(spec.type, normalizeSpec(spec));
  }
  return {
    raw: catalog || null,
    version: catalog?.version || null,
    graphSchema: catalog?.graph_schema || GRAPH_SCHEMA,
    dataTypes: catalog?.data_types || {},
    categories: Array.isArray(catalog?.categories) ? catalog.categories : [],
    nodes: [...types.values()],
    types,
    get: (type) => types.get(type) || null,
    has: (type) => types.has(type),
  };
}

export function inputPort(spec, key) {
  return spec?.inputs.find((port) => port.key === key) || null;
}

export function outputPort(spec, key) {
  return spec?.outputs.find((port) => port.key === key) || null;
}

/** The data a new node starts with: every parameter that has a default. */
export function defaultData(spec) {
  const data = {};
  for (const param of spec?.params || []) {
    if (param.default !== null && param.default !== undefined) data[param.key] = param.default;
  }
  return data;
}

const EMPTY_IS_UNSET = new Set(["number", "integer", "connection", "model", "asset"]);
const EMPTY_IS_DEFAULT = new Set(["select", "boolean"]);

/** The value the server will store for a parameter (graph.py `normalize_param`). */
export function effectiveValue(param, data) {
  const value = data?.[param.key];
  if (value === undefined || value === null) return param.default ?? null;
  if (value === "" && EMPTY_IS_DEFAULT.has(param.kind)) return param.default ?? null;
  if (value === "" && EMPTY_IS_UNSET.has(param.kind)) return null;
  return value;
}

export function effectiveData(spec, data) {
  const result = {};
  for (const param of spec?.params || []) result[param.key] = effectiveValue(param, data);
  return result;
}

/** `visible_when: {source: ["connection"]}` holds when every key has one of its values. */
export function paramVisible(param, effective) {
  const rules = param?.visible_when;
  if (!rules || typeof rules !== "object") return true;
  return Object.entries(rules).every(([key, values]) => Array.isArray(values) && values.includes(effective?.[key]));
}

export function visibleParams(spec, data) {
  const effective = effectiveData(spec, data);
  return (spec?.params || []).filter((param) => paramVisible(param, effective));
}

/* ------------------------------------------------------------------ ids */

function randomToken(length) {
  const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789";
  const bytes = new Uint8Array(length);
  if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes);
  else for (let index = 0; index < length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  return Array.from(bytes, (byte) => alphabet[byte % alphabet.length]).join("");
}

export function newId(prefix = "n", taken = null) {
  for (;;) {
    const id = `${prefix}_${randomToken(10)}`;
    if (!taken || !taken.has(id)) return id;
  }
}

/* ------------------------------------------------------------------ document */

function cleanData(data) {
  const result = {};
  if (!data || typeof data !== "object" || Array.isArray(data)) return result;
  for (const [key, value] of Object.entries(data)) {
    if (value !== undefined) result[key] = value;
  }
  return result;
}

function makeNode({ id, type, position, data, label }) {
  const x = finite(position?.x) ? clamp(Math.round(position.x), -COORDINATE_LIMIT, COORDINATE_LIMIT) : 0;
  const y = finite(position?.y) ? clamp(Math.round(position.y), -COORDINATE_LIMIT, COORDINATE_LIMIT) : 0;
  // An unchanged part keeps its identity, so a move is not a data change.
  const samePosition = position && Object.isFrozen(position) && position.x === x && position.y === y;
  const node = {
    id,
    type,
    position: samePosition ? position : Object.freeze({ x, y }),
    data: data && Object.isFrozen(data) ? data : Object.freeze(cleanData(data)),
  };
  const title = typeof label === "string" ? label.replace(/\s+/g, " ").trim().slice(0, MAX_LABEL) : "";
  if (title) node.label = title;
  return Object.freeze(node);
}

function makeEdge({ id, source, source_port: sourcePort, target, target_port: targetPort }) {
  return Object.freeze({ id, source, source_port: sourcePort, target, target_port: targetPort });
}

export function clampViewport(viewport) {
  const zoom = finite(viewport?.zoom) ? clamp(viewport.zoom, ZOOM_MIN, ZOOM_MAX) : 1;
  const x = finite(viewport?.x) ? clamp(viewport.x, -COORDINATE_LIMIT, COORDINATE_LIMIT) : 0;
  const y = finite(viewport?.y) ? clamp(viewport.y, -COORDINATE_LIMIT, COORDINATE_LIMIT) : 0;
  return Object.freeze({ x, y, zoom });
}

/**
 * A loaded document, made safe to render: unusable nodes and dangling edges
 * are dropped. Unknown node types are kept (their data is the user's), and
 * the editor draws them as unknown.
 */
export function normalizeDocument(input) {
  const source = input && typeof input === "object" ? input : {};
  const nodes = [];
  const ids = new Set();
  for (const raw of Array.isArray(source.nodes) ? source.nodes : []) {
    if (!raw || typeof raw !== "object" || typeof raw.id !== "string" || !ID_PATTERN.test(raw.id)) continue;
    if (ids.has(raw.id) || typeof raw.type !== "string") continue;
    ids.add(raw.id);
    nodes.push(makeNode({ id: raw.id, type: raw.type, position: raw.position, data: raw.data, label: raw.label }));
  }
  const edges = [];
  const edgeIds = new Set();
  for (const raw of Array.isArray(source.edges) ? source.edges : []) {
    if (!raw || typeof raw !== "object" || typeof raw.id !== "string" || !ID_PATTERN.test(raw.id)) continue;
    if (edgeIds.has(raw.id) || !ids.has(raw.source) || !ids.has(raw.target)) continue;
    if (typeof raw.source_port !== "string" || typeof raw.target_port !== "string") continue;
    edgeIds.add(raw.id);
    edges.push(makeEdge(raw));
  }
  return { schema: GRAPH_SCHEMA, nodes, edges, viewport: clampViewport(source.viewport) };
}

export function documentToJSON(doc) {
  return {
    schema: GRAPH_SCHEMA,
    nodes: doc.nodes.map((node) => {
      const item = {
        id: node.id,
        type: node.type,
        position: { x: node.position.x, y: node.position.y },
        data: { ...node.data },
      };
      if (node.label) item.label = node.label;
      return item;
    }),
    edges: doc.edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      source_port: edge.source_port,
      target: edge.target,
      target_port: edge.target_port,
    })),
    viewport: { x: doc.viewport.x, y: doc.viewport.y, zoom: doc.viewport.zoom },
  };
}

/* ------------------------------------------------------------------ graph queries */

function adjacency(edges, direction) {
  const map = new Map();
  for (const edge of edges) {
    const from = direction === "out" ? edge.source : edge.target;
    const to = direction === "out" ? edge.target : edge.source;
    if (!map.has(from)) map.set(from, []);
    map.get(from).push(to);
  }
  return map;
}

function walk(doc, ids, direction) {
  const present = new Set(doc.nodes.map((node) => node.id));
  const next = adjacency(doc.edges, direction);
  const result = new Set();
  const stack = [...ids].filter((id) => present.has(id));
  while (stack.length) {
    const current = stack.pop();
    if (result.has(current)) continue;
    result.add(current);
    for (const neighbour of next.get(current) || []) stack.push(neighbour);
  }
  return result;
}

/** The given nodes and everything that feeds them, transitively. */
export function upstreamOf(doc, ids) {
  return walk(doc, ids, "in");
}

/** The given nodes and everything they feed, transitively. */
export function downstreamOf(doc, ids) {
  return walk(doc, ids, "out");
}

/** Kahn's order, or null when the edges hold a cycle. */
export function topologicalOrder(doc) {
  const indegree = new Map(doc.nodes.map((node) => [node.id, 0]));
  const outgoing = new Map(doc.nodes.map((node) => [node.id, []]));
  for (const edge of doc.edges) {
    if (!indegree.has(edge.source) || !indegree.has(edge.target)) continue;
    indegree.set(edge.target, indegree.get(edge.target) + 1);
    outgoing.get(edge.source).push(edge.target);
  }
  const queue = doc.nodes.filter((node) => indegree.get(node.id) === 0).map((node) => node.id);
  const order = [];
  while (queue.length) {
    const current = queue.shift();
    order.push(current);
    for (const target of outgoing.get(current)) {
      indegree.set(target, indegree.get(target) - 1);
      if (indegree.get(target) === 0) queue.push(target);
    }
  }
  return order.length === doc.nodes.length ? order : null;
}

function reaches(edges, from, to, ignoreEdgeId = null) {
  const next = new Map();
  for (const edge of edges) {
    if (edge.id === ignoreEdgeId) continue;
    if (!next.has(edge.source)) next.set(edge.source, []);
    next.get(edge.source).push(edge.target);
  }
  const seen = new Set();
  const stack = [from];
  while (stack.length) {
    const current = stack.pop();
    if (current === to) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    for (const neighbour of next.get(current) || []) stack.push(neighbour);
  }
  return false;
}

function dataTypeLabel(catalog, dataType) {
  return catalog?.dataTypes?.[dataType]?.label || dataType;
}

const refuse = (reason, message) => ({ ok: false, reason, message });

/**
 * Whether `source.source_port -> target.target_port` may be added.
 *
 * `{ok: true, replaces}`: `replaces` is the id of the edge a single-capacity
 * input already holds, which `connect` swaps out. Every refusal carries a
 * `reason` code and a sentence for the user.
 */
export function checkConnection(doc, catalog, candidate) {
  const index = indexCatalog(catalog);
  const { source, source_port: sourcePort, target, target_port: targetPort } = candidate || {};
  const sourceNode = doc.nodes.find((node) => node.id === source);
  const targetNode = doc.nodes.find((node) => node.id === target);
  if (!sourceNode || !targetNode) return refuse("missing-node", "That node is no longer on the canvas.");
  if (source === target) return refuse("self", "A node cannot connect to itself.");
  const output = outputPort(index.get(sourceNode.type), sourcePort);
  const input = inputPort(index.get(targetNode.type), targetPort);
  if (!output) return refuse("unknown-output", "That output does not exist.");
  if (!input) return refuse("unknown-input", "That input does not exist.");
  if (output.data_type !== input.data_type) {
    return refuse(
      "type",
      `${dataTypeLabel(index, output.data_type)} cannot connect to a ${String(dataTypeLabel(index, input.data_type)).toLowerCase()} input.`,
    );
  }
  const into = doc.edges.filter((edge) => edge.target === target && edge.target_port === targetPort);
  if (into.some((edge) => edge.source === source && edge.source_port === sourcePort)) {
    return refuse("duplicate", "These ports are already connected.");
  }
  let replaces = null;
  if (!input.multiple) {
    replaces = into[0]?.id || null;
  } else {
    const limit = Math.max(1, Number(input.max_connections) || 1);
    if (into.length >= limit) {
      return refuse("capacity", `${input.label} accepts at most ${limit} connection${limit === 1 ? "" : "s"}.`);
    }
  }
  if (reaches(doc.edges, target, source, replaces)) {
    return refuse("cycle", "That connection would make a loop.");
  }
  return { ok: true, replaces };
}

/**
 * Every port on the canvas a drag from `from` could end on, with its verdict.
 * `from` is `{nodeId, port, direction: "out" | "in"}`; the result is keyed
 * `"<nodeId>:<portKey>"` over the ports of the opposite direction.
 */
export function connectionCandidates(doc, catalog, from) {
  const index = indexCatalog(catalog);
  const result = new Map();
  for (const node of doc.nodes) {
    const spec = index.get(node.type);
    if (!spec) continue;
    const ports = from.direction === "out" ? spec.inputs : spec.outputs;
    for (const port of ports) {
      const candidate = from.direction === "out"
        ? { source: from.nodeId, source_port: from.port, target: node.id, target_port: port.key }
        : { source: node.id, source_port: port.key, target: from.nodeId, target_port: from.port };
      result.set(`${node.id}:${port.key}`, checkConnection(doc, index, candidate));
    }
  }
  return result;
}

/* ------------------------------------------------------------------ run rules */

const textSet = (value) => typeof value === "string" && value.trim() !== "";

/**
 * What a run would execute: `targets` are the nodes asked for (every
 * executable node when none are named), `scope` adds whatever feeds them.
 * Notes and unknown types never run.
 */
export function runScope(doc, catalog, nodeIds = null) {
  const index = indexCatalog(catalog);
  const executable = new Set(
    doc.nodes.filter((node) => index.get(node.type)?.executable).map((node) => node.id),
  );
  const order = topologicalOrder(doc) || doc.nodes.map((node) => node.id);
  if (Array.isArray(nodeIds) && nodeIds.length) {
    const targets = [...new Set(nodeIds)].filter((id) => executable.has(id));
    const upstream = upstreamOf(doc, targets);
    return { targets, scope: order.filter((id) => upstream.has(id) && executable.has(id)) };
  }
  const all = order.filter((id) => executable.has(id));
  return { targets: all, scope: all };
}

/**
 * Every problem a run over `nodeIds` (all nodes when omitted) would be
 * refused for, one entry per problem: `{node_id, param | port, message}`.
 */
export function validateForRun(doc, catalog, nodeIds = null) {
  const index = indexCatalog(catalog);
  const { scope } = runScope(doc, index, nodeIds);
  const byId = new Map(doc.nodes.map((node) => [node.id, node]));
  const issues = [];
  const connected = (nodeId, port) => doc.edges.some((edge) => edge.target === nodeId && edge.target_port === port);
  for (const nodeId of scope) {
    const node = byId.get(nodeId);
    const spec = index.get(node.type);
    if (!spec || !spec.executable) continue;
    const data = effectiveData(spec, node.data);
    for (const param of spec.params) {
      if (!param.required || !paramVisible(param, data)) continue;
      const value = data[param.key];
      if (value === null || value === undefined || (typeof value === "string" && !value.trim())) {
        issues.push({ node_id: nodeId, param: param.key, message: `${param.label} is required` });
      }
    }
    for (const port of spec.inputs) {
      if (port.required && !connected(nodeId, port.key)) {
        issues.push({ node_id: nodeId, port: port.key, message: `Connect something to ${port.label}` });
      }
    }
    const promptConnected = inputPort(spec, "prompt") ? connected(nodeId, "prompt") : false;
    if (node.type === "llm" && !promptConnected && !textSet(data.instruction)) {
      issues.push({ node_id: nodeId, param: "instruction", message: "Write an instruction or connect text to Prompt" });
    }
    if (node.type === "image_generation" || node.type === "video_generation") {
      if (!promptConnected && !textSet(data.prompt)) {
        issues.push({ node_id: nodeId, param: "prompt", message: "Write a prompt or connect text to Prompt" });
      }
      if (data.source === "connection") {
        for (const [key, label] of [["connection_id", "Connection"], ["model", "Model"]]) {
          if (!textSet(data[key])) issues.push({ node_id: nodeId, param: key, message: `${label} is required` });
        }
      }
    }
    if (node.type === "video_generation" && connected(nodeId, "last_frame") && !connected(nodeId, "first_frame")) {
      issues.push({ node_id: nodeId, port: "last_frame", message: "A last frame needs a first frame" });
    }
  }
  return issues;
}

/* ------------------------------------------------------------------ clipboard */

export function isClipboardPayload(payload) {
  return Boolean(payload && payload.type === CLIPBOARD_TYPE && Array.isArray(payload.nodes));
}

export function parseClipboardText(text) {
  if (typeof text !== "string" || !text.includes(CLIPBOARD_TYPE)) return null;
  try {
    const payload = JSON.parse(text);
    return isClipboardPayload(payload) ? payload : null;
  } catch (_error) {
    return null;
  }
}

/* ------------------------------------------------------------------ store */

export class GraphStore {
  constructor({ catalog, document = null, historyLimit = HISTORY_LIMIT } = {}) {
    this.catalog = indexCatalog(catalog);
    this.historyLimit = historyLimit;
    this.doc = normalizeDocument(document);
    this.lastError = null;
    this._listeners = new Set();
    this._undo = [];
    this._redo = [];
    this._batchDepth = 0;
    this._batchBefore = null;
    this._coalesce = null;
    this._indexFor = null;
    this._index = null;
  }

  /* ---- reading ---- */

  get nodes() { return this.doc.nodes; }
  get edges() { return this.doc.edges; }
  get viewport() { return this.doc.viewport; }
  get canUndo() { return this._undo.length > 0; }
  get canRedo() { return this._redo.length > 0; }

  getNode(id) {
    if (this._indexFor !== this.doc.nodes) {
      this._index = new Map(this.doc.nodes.map((node) => [node.id, node]));
      this._indexFor = this.doc.nodes;
    }
    return this._index.get(id) || null;
  }

  getEdge(id) {
    return this.doc.edges.find((edge) => edge.id === id) || null;
  }

  specOf(node) {
    const type = typeof node === "string" ? this.getNode(node)?.type : node?.type;
    return this.catalog.get(type);
  }

  incoming(nodeId, port = null) {
    return this.doc.edges.filter((edge) => edge.target === nodeId && (port === null || edge.target_port === port));
  }

  outgoing(nodeId, port = null) {
    return this.doc.edges.filter((edge) => edge.source === nodeId && (port === null || edge.source_port === port));
  }

  toJSON() {
    return documentToJSON(this.doc);
  }

  /* ---- events ---- */

  subscribe(listener) {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  }

  _emit(change) {
    const event = { docChanged: true, origin: "user", ...change, canUndo: this.canUndo, canRedo: this.canRedo };
    for (const listener of [...this._listeners]) listener(event);
  }

  _fail(message) {
    this.lastError = message;
    return null;
  }

  /* ---- history ---- */

  _snapshot() {
    return { nodes: this.doc.nodes, edges: this.doc.edges };
  }

  _pushUndo(snapshot) {
    this._undo.push(snapshot);
    if (this._undo.length > this.historyLimit) this._undo.shift();
    this._redo = [];
  }

  _commit(before, coalesceKey = null) {
    if (this._batchDepth > 0) return;
    if (before.nodes === this.doc.nodes && before.edges === this.doc.edges) return;
    const now = Date.now();
    if (coalesceKey && this._coalesce?.key === coalesceKey && now - this._coalesce.at < COALESCE_MS) {
      this._coalesce.at = now;
      return;
    }
    this._pushUndo(before);
    this._coalesce = coalesceKey ? { key: coalesceKey, at: now } : null;
  }

  /** Everything until the matching `endBatch` is one undo step (a drag, a paste). */
  beginBatch() {
    if (this._batchDepth === 0) this._batchBefore = this._snapshot();
    this._batchDepth += 1;
  }

  endBatch() {
    if (this._batchDepth === 0) return;
    this._batchDepth -= 1;
    if (this._batchDepth > 0) return;
    const before = this._batchBefore;
    this._batchBefore = null;
    if (before && (before.nodes !== this.doc.nodes || before.edges !== this.doc.edges)) {
      this._pushUndo(before);
      this._coalesce = null;
      this._emit({ kind: "history", docChanged: false });
    }
  }

  /** Abandon the open batch: the document returns to where the batch began. */
  cancelBatch() {
    if (this._batchDepth === 0) return;
    const before = this._batchBefore;
    this._batchDepth = 0;
    this._batchBefore = null;
    if (before && (before.nodes !== this.doc.nodes || before.edges !== this.doc.edges)) {
      this.doc = { ...this.doc, nodes: before.nodes, edges: before.edges };
      this._emit({ kind: "replace", origin: "cancel" });
    }
  }

  undo() {
    if (this._batchDepth > 0 || !this._undo.length) return false;
    const target = this._undo.pop();
    this._redo.push(this._snapshot());
    this.doc = { ...this.doc, nodes: target.nodes, edges: target.edges };
    this._coalesce = null;
    this._emit({ kind: "replace", origin: "undo" });
    return true;
  }

  redo() {
    if (this._batchDepth > 0 || !this._redo.length) return false;
    const target = this._redo.pop();
    this._undo.push(this._snapshot());
    this.doc = { ...this.doc, nodes: target.nodes, edges: target.edges };
    this._coalesce = null;
    this._emit({ kind: "replace", origin: "redo" });
    return true;
  }

  /** Replace the whole document (a load or a reload). History starts over. */
  load(document) {
    this.doc = normalizeDocument(document);
    this._undo = [];
    this._redo = [];
    this._coalesce = null;
    this._batchDepth = 0;
    this._batchBefore = null;
    this._emit({ kind: "replace", origin: "load", docChanged: false });
  }

  /* ---- mutations ---- */

  _replaceNodes(updates) {
    this.doc = { ...this.doc, nodes: this.doc.nodes.map((node) => updates.get(node.id) || node) };
  }

  _takenIds(kind = "nodes") {
    return new Set((kind === "nodes" ? this.doc.nodes : this.doc.edges).map((item) => item.id));
  }

  addNode(type, position = { x: 0, y: 0 }, data = {}, { label, id } = {}) {
    const spec = this.catalog.get(type);
    if (!spec) return this._fail(`Unknown node type: ${type}`);
    if (this.doc.nodes.length >= MAX_NODES) return this._fail(`A canvas can have at most ${MAX_NODES} nodes.`);
    const taken = this._takenIds();
    const nodeId = typeof id === "string" && ID_PATTERN.test(id) && !taken.has(id) ? id : newId("n", taken);
    const node = makeNode({ id: nodeId, type, position, data: { ...defaultData(spec), ...cleanData(data) }, label });
    const before = this._snapshot();
    this.doc = { ...this.doc, nodes: [...this.doc.nodes, node] };
    this._commit(before);
    this._emit({ kind: "add", nodeIds: [nodeId], edgeIds: [] });
    return node;
  }

  updateNodeData(id, patch, { coalesce = false } = {}) {
    const node = this.getNode(id);
    if (!node || !patch || typeof patch !== "object") return null;
    const keys = Object.keys(patch).filter((key) => !Object.is(node.data[key], patch[key]));
    if (!keys.length) return node;
    const data = { ...node.data };
    for (const key of keys) {
      if (patch[key] === undefined) delete data[key];
      else data[key] = patch[key];
    }
    const next = makeNode({ ...node, data });
    const before = this._snapshot();
    this._replaceNodes(new Map([[id, next]]));
    this._commit(before, coalesce ? `data:${id}:${[...keys].sort().join(",")}` : null);
    this._emit({ kind: "data", nodeIds: [id], keys });
    return next;
  }

  renameNode(id, label) {
    const node = this.getNode(id);
    if (!node) return null;
    const title = typeof label === "string" ? label.replace(/\s+/g, " ").trim().slice(0, MAX_LABEL) : "";
    if ((node.label || "") === title) return node;
    const { label: _previous, ...rest } = node;
    const next = makeNode({ ...rest, label: title });
    const before = this._snapshot();
    this._replaceNodes(new Map([[id, next]]));
    this._commit(before);
    this._emit({ kind: "label", nodeIds: [id] });
    return next;
  }

  /** Absolute positions: `[{id, x, y}]` or a Map of id -> {x, y}. */
  setPositions(positions) {
    const entries = positions instanceof Map
      ? [...positions.entries()].map(([id, point]) => ({ id, ...point }))
      : [...(positions || [])];
    const updates = new Map();
    for (const { id, x, y } of entries) {
      const node = this.getNode(id);
      if (!node || !finite(x) || !finite(y)) continue;
      if (Math.round(x) === node.position.x && Math.round(y) === node.position.y) continue;
      updates.set(id, makeNode({ ...node, position: { x, y } }));
    }
    if (!updates.size) return [];
    const before = this._snapshot();
    this._replaceNodes(updates);
    this._commit(before);
    const nodeIds = [...updates.keys()];
    this._emit({ kind: "move", nodeIds });
    return nodeIds;
  }

  moveNodes(ids, dx, dy) {
    if (!finite(dx) || !finite(dy) || (!dx && !dy)) return [];
    return this.setPositions(
      [...new Set(ids)].map((id) => this.getNode(id)).filter(Boolean)
        .map((node) => ({ id: node.id, x: node.position.x + dx, y: node.position.y + dy })),
    );
  }

  removeNodes(ids) {
    const doomed = new Set([...(ids || [])].filter((id) => this.getNode(id)));
    if (!doomed.size) return { nodeIds: [], edgeIds: [] };
    const edgeIds = [];
    const touched = new Set();
    for (const edge of this.doc.edges) {
      if (doomed.has(edge.source) || doomed.has(edge.target)) {
        edgeIds.push(edge.id);
        touched.add(edge.source);
        touched.add(edge.target);
      }
    }
    const before = this._snapshot();
    this.doc = {
      ...this.doc,
      nodes: this.doc.nodes.filter((node) => !doomed.has(node.id)),
      edges: this.doc.edges.filter((edge) => !edgeIds.includes(edge.id)),
    };
    this._commit(before);
    const nodeIds = [...doomed];
    this._emit({
      kind: "remove",
      nodeIds,
      edgeIds,
      touchedNodeIds: [...touched].filter((id) => !doomed.has(id)),
    });
    return { nodeIds, edgeIds };
  }

  removeEdges(ids) {
    const doomed = new Set(ids || []);
    const removed = this.doc.edges.filter((edge) => doomed.has(edge.id));
    if (!removed.length) return [];
    const before = this._snapshot();
    this.doc = { ...this.doc, edges: this.doc.edges.filter((edge) => !doomed.has(edge.id)) };
    this._commit(before);
    const touched = new Set(removed.flatMap((edge) => [edge.source, edge.target]));
    this._emit({ kind: "edges", added: [], removed: removed.map((edge) => edge.id), nodeIds: [...touched] });
    return removed.map((edge) => edge.id);
  }

  canConnect(candidate) {
    return checkConnection(this.doc, this.catalog, candidate);
  }

  /** Add an edge; a single-capacity input gives up the edge it held. */
  connect(candidate) {
    const check = this.canConnect(candidate);
    if (!check.ok) return check;
    if (this.doc.edges.length - (check.replaces ? 1 : 0) >= MAX_EDGES) {
      return refuse("limit", `A canvas can have at most ${MAX_EDGES} connections.`);
    }
    const replaced = check.replaces ? this.getEdge(check.replaces) : null;
    const edge = makeEdge({
      id: newId("e", this._takenIds("edges")),
      source: candidate.source,
      source_port: candidate.source_port,
      target: candidate.target,
      target_port: candidate.target_port,
    });
    const before = this._snapshot();
    this.doc = {
      ...this.doc,
      edges: [...this.doc.edges.filter((item) => item.id !== check.replaces), edge],
    };
    this._commit(before);
    const touched = new Set([edge.source, edge.target]);
    if (replaced) touched.add(replaced.source);
    this._emit({ kind: "edges", added: [edge.id], removed: replaced ? [replaced.id] : [], nodeIds: [...touched] });
    return { ok: true, edge, replaced: replaced?.id || null };
  }

  setViewport(viewport) {
    const next = clampViewport({ ...this.doc.viewport, ...viewport });
    const current = this.doc.viewport;
    if (next.x === current.x && next.y === current.y && next.zoom === current.zoom) return;
    this.doc = { ...this.doc, viewport: next };
    this._emit({ kind: "viewport", docChanged: false });
  }

  /** The selected nodes and the edges between them, as a detached payload. */
  copy(ids) {
    const chosen = new Set(ids || []);
    const nodes = this.doc.nodes.filter((node) => chosen.has(node.id));
    if (!nodes.length) return null;
    const edges = this.doc.edges.filter((edge) => chosen.has(edge.source) && chosen.has(edge.target));
    return JSON.parse(JSON.stringify({ type: CLIPBOARD_TYPE, version: 1, nodes, edges }));
  }

  /**
   * Add a copied payload under fresh ids. `at` puts the payload's top-left
   * corner at a world point; otherwise it lands `offset` from where it was.
   * Returns the new node ids.
   */
  paste(payload, { at = null, offset = { x: 40, y: 40 } } = {}) {
    if (!isClipboardPayload(payload)) return [];
    const usable = payload.nodes.filter((node) => node && typeof node.id === "string"
      && this.catalog.get(node.type) && finite(node.position?.x) && finite(node.position?.y));
    if (!usable.length) return [];
    const room = MAX_NODES - this.doc.nodes.length;
    if (room <= 0) {
      this._fail(`A canvas can have at most ${MAX_NODES} nodes.`);
      return [];
    }
    const chosen = usable.slice(0, room);
    const minX = Math.min(...chosen.map((node) => node.position.x));
    const minY = Math.min(...chosen.map((node) => node.position.y));
    const dx = at && finite(at.x) ? at.x - minX : (offset?.x || 0);
    const dy = at && finite(at.y) ? at.y - minY : (offset?.y || 0);
    const taken = this._takenIds();
    const renamed = new Map();
    const created = chosen.map((node) => {
      const id = newId("n", taken);
      taken.add(id);
      renamed.set(node.id, id);
      return makeNode({
        id,
        type: node.type,
        position: { x: node.position.x + dx, y: node.position.y + dy },
        data: node.data,
        label: node.label,
      });
    });
    const before = this._snapshot();
    this.doc = { ...this.doc, nodes: [...this.doc.nodes, ...created] };
    const edgeIds = this._takenIds("edges");
    const added = [];
    let edges = this.doc.edges;
    for (const raw of Array.isArray(payload.edges) ? payload.edges : []) {
      const source = renamed.get(raw?.source);
      const target = renamed.get(raw?.target);
      if (!source || !target || edges.length >= MAX_EDGES) continue;
      const candidate = { source, source_port: raw.source_port, target, target_port: raw.target_port };
      const check = checkConnection({ ...this.doc, edges }, this.catalog, candidate);
      if (!check.ok || check.replaces) continue;
      const edge = makeEdge({ id: newId("e", edgeIds), ...candidate });
      edgeIds.add(edge.id);
      edges = [...edges, edge];
      added.push(edge.id);
    }
    this.doc = { ...this.doc, edges };
    this._commit(before);
    const nodeIds = created.map((node) => node.id);
    this._emit({ kind: "add", nodeIds, edgeIds: added });
    return nodeIds;
  }

  duplicate(ids, offset = { x: 40, y: 40 }) {
    return this.paste(this.copy(ids), { offset });
  }
}

/* ------------------------------------------------------------------ starter canvas */

export const STARTER_PROMPT = "A lone astronaut walks through a neon-lit night market, cinematic, slow push-in";
export const STARTER_NOTE = "Add your own model API keys in API connections, then switch a node's Run on to My API connection.";

/** "My first canvas": a text prompt wired into a video node, and a note on connections. */
export function starterGraph(catalog) {
  const index = indexCatalog(catalog);
  const store = new GraphStore({ catalog: index });
  const textSpec = index.get("text");
  const videoSpec = index.get("video_generation");
  const text = textSpec ? store.addNode("text", { x: 0, y: 40 }, { text: STARTER_PROMPT }) : null;
  const video = videoSpec ? store.addNode("video_generation", { x: 400, y: 0 }, { source: "platform" }) : null;
  if (index.get("note")) store.addNode("note", { x: 0, y: 300 }, { text: STARTER_NOTE });
  if (text && video) {
    const output = textSpec.outputs[0];
    const input = inputPort(videoSpec, "prompt")
      || videoSpec.inputs.find((port) => port.data_type === output?.data_type);
    if (output && input) {
      store.connect({ source: text.id, source_port: output.key, target: video.id, target_port: input.key });
    }
  }
  return store.toJSON();
}
