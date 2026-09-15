/**
 * Node cards: the DOM for one node, kept in step with the document, the
 * node's latest run and its validation errors.
 *
 * Geometry is fixed so edges never need to measure the DOM: a 44px header,
 * then one 26px row per port (inputs down the left edge, outputs down the
 * right), then the body. `portAnchor` computes a port's world position from
 * those constants, which canvas.css enforces.
 */
import { effectiveData, paramVisible } from "./graph.js";
import { createField } from "./fields.js";
import { icon, hasIcon } from "./icons.js";
import { clockTime, escapeHTML, friendlyModel, parsePlatformModel } from "./ui.js";

export const NODE_WIDTH = 300;
export const NOTE_WIDTH = 260;
export const HEADER_HEIGHT = 44;
export const PORT_ROW = 26;
export const PORTS_PAD = 8;

export const nodeWidth = (node) => (node?.type === "note" ? NOTE_WIDTH : NODE_WIDTH);

export function portRows(spec) {
  return Math.max(spec?.inputs?.length || 0, spec?.outputs?.length || 0);
}

/** A port's centre in world coordinates. */
export function portAnchor(node, spec, direction, key) {
  const ports = direction === "out" ? spec?.outputs : spec?.inputs;
  const index = Math.max(0, (ports || []).findIndex((port) => port.key === key));
  return {
    x: node.position.x + (direction === "out" ? nodeWidth(node) : 0),
    y: node.position.y + HEADER_HEIGHT + PORTS_PAD + index * PORT_ROW + PORT_ROW / 2,
  };
}

export const RUN_STATUS = {
  PENDING: { label: "Queued", tone: "queued" },
  RUNNING: { label: "Running", tone: "running" },
  SUCCEEDED: { label: "Done", tone: "ok", icon: "check" },
  CACHED: { label: "Reused", tone: "ok", icon: "check", title: "Reused an earlier result for exactly these inputs, so nothing new was charged." },
  FAILED: { label: "Failed", tone: "danger", icon: "alert" },
  SKIPPED: { label: "Skipped", tone: "neutral", title: "An input this node needs did not finish." },
  CANCELLED: { label: "Cancelled", tone: "neutral" },
};

const TOP_UP_CODES = new Set(["INSUFFICIENT_CREDITS", "PLAN_DENIED"]);

const DATA_TYPE_CLASS = { text: "t-text", image: "t-image", video: "t-video" };
export const dataTypeClass = (dataType) => DATA_TYPE_CLASS[dataType] || "t-other";

export function nodeTitle(node, spec) {
  return node?.label || spec?.title || (node ? `Unknown node (${node.type})` : "Node");
}

export function nodeIcon(spec) {
  return spec && hasIcon(spec.icon) ? spec.icon : (spec ? "square" : "alert");
}

/** The one-line "how this runs" summary under a generation node. */
export function runSummary(node, spec, env) {
  if (!spec) return "";
  const data = effectiveData(spec, node.data);
  const connectionName = (id) => (env.connections || []).find((item) => item.id === id)?.name;
  if (node.type === "llm" || data.source === "connection") {
    if (!spec.params.some((param) => param.kind === "connection")) return "";
    const name = data.connection_id ? (connectionName(data.connection_id) || "Missing connection") : "Choose a connection";
    return `${name} · ${data.model || "choose a model"}`;
  }
  if (data.source !== "platform") return "";
  const parts = ["BestShiny credits"];
  if (node.type === "video_generation") {
    const chosen = parsePlatformModel(data.platform_model);
    parts.push(chosen ? friendlyModel(chosen.modelId) : "Automatic");
    if (data.duration) parts.push(`${data.duration}s`);
    if (data.aspect_ratio) parts.push(data.aspect_ratio);
  } else {
    const tier = spec.params.find((param) => param.key === "image_tier");
    const option = tier?.options?.find((item) => item.value === data.image_tier);
    if (option) parts.push(option.label);
    if (data.aspect_ratio) parts.push(data.aspect_ratio);
  }
  return parts.join(" · ");
}

function outputOf(spec, outputs) {
  for (const port of spec?.outputs || []) {
    const value = outputs?.[port.key];
    if (value === undefined || value === null) continue;
    if (port.data_type === "text") return { dataType: "text", text: String(value) };
    if (value && typeof value === "object" && value.asset_id) {
      return { dataType: port.data_type, assetId: value.asset_id, jobId: value.generation_job_id || null };
    }
  }
  return null;
}

export class NodeCards {
  constructor(layer, env, editor) {
    this.layer = layer;
    this.env = env;
    this.editor = editor;
    this.records = new Map();
    this.layer.addEventListener("click", (event) => this.onClick(event));
    this.layer.addEventListener("dblclick", (event) => this.onDoubleClick(event));
    this.layer.addEventListener("change", (event) => this.onFileChange(event));
  }

  get store() {
    return this.env.store;
  }

  get(id) {
    return this.records.get(id) || null;
  }

  ids() {
    return [...this.records.keys()];
  }

  /* ---------------------------------------------------------------- lifecycle */

  create(node) {
    const spec = this.store.catalog.get(node.type);
    const el = document.createElement("div");
    el.className = `cv-node${spec ? "" : " is-unknown"}${node.type === "note" ? " is-note" : ""}`;
    el.dataset.cvNode = node.id;
    el.dataset.nodeType = node.type;
    el.tabIndex = 0;
    el.setAttribute("role", "group");
    el.style.width = `${nodeWidth(node)}px`;
    el.innerHTML = `
      <div class="cv-node-head"></div>
      <div class="cv-node-progress" hidden><i></i></div>
      <div class="cv-node-ports"></div>
      <div class="cv-node-body"></div>
      <div class="cv-node-result" hidden></div>
      <ul class="cv-node-errors" hidden></ul>
      <div class="cv-node-foot" hidden></div>`;
    const record = {
      id: node.id,
      node,
      spec,
      el,
      head: el.querySelector(".cv-node-head"),
      progress: el.querySelector(".cv-node-progress"),
      ports: el.querySelector(".cv-node-ports"),
      body: el.querySelector(".cv-node-body"),
      result: el.querySelector(".cv-node-result"),
      errors: el.querySelector(".cv-node-errors"),
      foot: el.querySelector(".cv-node-foot"),
      fields: new Map(),
      bodyKey: null,
      resultKey: null,
    };
    this.records.set(node.id, record);
    this.position(record);
    this.renderHead(record);
    this.renderPorts(record);
    this.renderBody(record);
    this.renderResult(record);
    this.renderErrors(record, { ports: false });
    this.renderFoot(record);
    this.layer.append(el);
    return record;
  }

  remove(id) {
    const record = this.records.get(id);
    if (!record) return;
    record.el.remove();
    this.records.delete(id);
  }

  clear() {
    for (const record of this.records.values()) record.el.remove();
    this.records.clear();
  }

  /** Bring a card up to date with a new node object. */
  sync(node) {
    const record = this.records.get(node.id);
    if (!record) return this.create(node);
    const previous = record.node;
    record.node = node;
    if (previous === node) return record;
    if (previous.position !== node.position) this.position(record);
    if (previous.label !== node.label) this.renderHead(record);
    if (previous.data !== node.data) {
      this.renderBody(record);
      this.renderFoot(record);
    }
    return record;
  }

  position(record) {
    const { x, y } = record.node.position;
    record.el.style.transform = `translate(${x}px, ${y}px)`;
  }

  /* ---------------------------------------------------------------- header */

  renderHead(record) {
    const { node, spec } = record;
    const title = nodeTitle(node, spec);
    const run = this.env.results.get(node.id);
    const status = run ? RUN_STATUS[run.status] : null;
    const executable = Boolean(spec?.executable);
    const readOnly = this.env.readOnly;
    record.el.setAttribute("aria-label", `${title}${status ? `, ${status.label}` : ""}`);
    record.head.innerHTML = `
      <span class="cv-node-icon ${spec?.category ? `c-${escapeHTML(spec.category)}` : ""}">${icon(nodeIcon(spec), { size: 15 })}</span>
      <span class="cv-node-title" title="${escapeHTML(title)}${readOnly ? "" : " — double-click to rename"}">${escapeHTML(title)}</span>
      ${status ? `<span class="cv-status tone-${status.tone}"${status.title ? ` title="${escapeHTML(status.title)}"` : ""}>${status.icon ? icon(status.icon, { size: 12 }) : "<i></i>"}<span>${status.label}</span></span>` : ""}
      ${executable ? `<button type="button" class="cv-node-btn cv-node-run" data-cv-action="run-node" title="Run this node" aria-label="Run this node"${readOnly || this.env.runActive ? " disabled" : ""}>${icon("play", { size: 13 })}</button>` : ""}
      <button type="button" class="cv-node-btn" data-cv-action="node-menu" title="More actions" aria-label="More actions for ${escapeHTML(title)}" aria-haspopup="menu">${icon("more", { size: 15 })}</button>`;
  }

  startRename(id) {
    const record = this.records.get(id);
    if (!record || this.env.readOnly) return;
    const titleEl = record.head.querySelector(".cv-node-title");
    if (!titleEl) return;
    const input = document.createElement("input");
    input.type = "text";
    input.className = "cv-node-rename";
    input.maxLength = 120;
    input.value = record.node.label || nodeTitle(record.node, record.spec);
    input.setAttribute("aria-label", "Node title");
    input.dataset.cvNodrag = "";
    titleEl.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const finish = (commit) => {
      if (done) return;
      done = true;
      if (commit) {
        const value = input.value.trim();
        const fallback = record.spec?.title || "";
        this.store.renameNode(id, value === fallback ? "" : value);
      }
      this.renderHead(this.records.get(id) || record);
    };
    input.addEventListener("keydown", (event) => {
      event.stopPropagation();
      if (event.key === "Enter") { event.preventDefault(); finish(true); }
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
  }

  /* ---------------------------------------------------------------- ports */

  renderPorts(record) {
    const { spec, node } = record;
    const rows = portRows(spec);
    record.ports.hidden = rows === 0;
    if (!rows) {
      record.ports.innerHTML = "";
      return;
    }
    record.ports.style.gridTemplateRows = `repeat(${rows}, ${PORT_ROW}px)`;
    const errors = this.env.errors.get(node.id) || [];
    const portMarkup = (port, index, direction) => {
      const edges = direction === "in" ? this.store.incoming(node.id, port.key) : this.store.outgoing(node.id, port.key);
      const connected = edges.length > 0;
      const invalid = direction === "in" && errors.some((issue) => issue.port === port.key);
      const missing = direction === "in" && port.required && !connected;
      const typeLabel = this.store.catalog.dataTypes?.[port.data_type]?.label || port.data_type;
      const count = direction === "in" && port.multiple
        ? `<span class="cv-port-count">${edges.length}/${port.max_connections}</span>`
        : "";
      const hint = direction === "in"
        ? `${port.label} · ${typeLabel} input${port.multiple ? `, up to ${port.max_connections}` : ""}${port.required ? ", required" : ""}`
        : `${port.label} · ${typeLabel} output — drag to connect`;
      return `<div class="cv-port is-${direction} ${dataTypeClass(port.data_type)}${connected ? " is-connected" : ""}${invalid ? " is-invalid" : ""}${missing ? " is-missing" : ""}"
        data-cv-port="${direction}" data-cv-port-key="${escapeHTML(port.key)}" data-data-type="${escapeHTML(port.data_type)}"
        style="grid-row:${index + 1}" title="${escapeHTML(hint)}">
        <span class="cv-port-dot" aria-hidden="true"></span>
        <span class="cv-port-label">${escapeHTML(port.label)}${port.required ? '<span class="cv-required" aria-hidden="true">*</span>' : ""}</span>${count}
      </div>`;
    };
    record.ports.innerHTML = [
      ...spec.inputs.map((port, index) => portMarkup(port, index, "in")),
      ...spec.outputs.map((port, index) => portMarkup(port, index, "out")),
    ].join("");
    if (this.editor?.candidates) this.editor.paintCandidates(record);
  }

  /* ---------------------------------------------------------------- body */

  connectedSource(nodeId, key) {
    const edge = this.store.incoming(nodeId, key)[0];
    if (!edge) return null;
    const source = this.store.getNode(edge.source);
    return source ? nodeTitle(source, this.store.catalog.get(source.type)) : null;
  }

  bodyStructure(record) {
    const { node, spec } = record;
    if (!spec) return "unknown";
    if (node.type === "image_input" || spec.params.some((param) => param.inline && param.kind === "asset")) {
      const upload = this.env.uploads.get(node.id);
      return `asset:${node.data.asset_id || ""}:${upload?.status || ""}:${upload?.message || ""}:${this.env.readOnly}`;
    }
    const data = effectiveData(spec, node.data);
    const keys = spec.params
      .filter((param) => param.inline && param.kind !== "asset" && paramVisible(param, data))
      .map((param) => {
        const connected = spec.inputs.some((port) => port.key === param.key) && this.store.incoming(node.id, param.key).length > 0;
        return `${param.key}${connected ? "+" : ""}`;
      });
    return `fields:${keys.join(",")}:${this.env.readOnly}`;
  }

  renderBody(record, { force = false } = {}) {
    const { node, spec } = record;
    const structure = this.bodyStructure(record);
    if (!force && structure === record.bodyKey) {
      this.syncFields(record);
      return;
    }
    record.bodyKey = structure;
    record.fields.clear();
    record.body.innerHTML = "";
    if (!spec) {
      record.body.innerHTML = `<p class="cv-node-note">This node type is not available any more. It stays on the canvas as it was saved.</p>`;
      return;
    }
    if (structure.startsWith("asset:")) {
      this.renderAssetBody(record);
      return;
    }
    const data = effectiveData(spec, node.data);
    const errors = this.env.errors.get(node.id) || [];
    const inline = spec.params.filter((param) => param.inline && paramVisible(param, data));
    record.body.hidden = inline.length === 0;
    for (const param of inline) {
      const hasPort = spec.inputs.some((port) => port.key === param.key);
      const connected = hasPort && this.store.incoming(node.id, param.key).length > 0;
      const field = createField(param, {
        compact: true,
        hideCompactLabel: node.type === "text" || node.type === "note",
        value: node.data[param.key] ?? data[param.key],
        data,
        disabled: this.env.readOnly,
        connected,
        connectedFrom: connected ? this.connectedSource(node.id, param.key) : null,
        env: this.env,
        onChange: (key, value, options) => this.store.updateNodeData(node.id, { [key]: value }, options),
      });
      field.setInvalid(errors.find((issue) => issue.param === param.key)?.message || null);
      record.fields.set(param.key, field);
      record.body.append(field.el);
    }
  }

  syncFields(record) {
    const { node, spec } = record;
    if (!spec || !record.fields.size) return;
    const data = effectiveData(spec, node.data);
    const errors = this.env.errors.get(node.id) || [];
    for (const [key, field] of record.fields) {
      const hasPort = spec.inputs.some((port) => port.key === key);
      const connected = hasPort && this.store.incoming(node.id, key).length > 0;
      field.sync(node.data[key] ?? data[key], {
        data,
        disabled: this.env.readOnly,
        connected,
        connectedFrom: connected ? this.connectedSource(node.id, key) : null,
        env: this.env,
      });
      field.setInvalid(errors.find((issue) => issue.param === key)?.message || null);
    }
  }

  renderAssetBody(record) {
    const { node } = record;
    const upload = this.env.uploads.get(node.id);
    const assetId = node.data.asset_id;
    const readOnly = this.env.readOnly;
    const errors = this.env.errors.get(node.id) || [];
    const invalid = errors.find((issue) => issue.param === "asset_id");
    record.body.hidden = false;
    const input = readOnly ? "" : '<input type="file" accept="image/png,image/jpeg,image/webp" data-cv-file hidden />';
    if (upload?.status === "uploading") {
      record.body.innerHTML = `<div class="cv-asset is-busy" data-surface="dark"><span class="cv-spinner" aria-hidden="true"></span><span>Uploading ${escapeHTML(upload.name || "image")}…</span></div>`;
      return;
    }
    if (upload?.status === "error") {
      record.body.innerHTML = `<div class="cv-asset is-error">
          ${icon("alert", { size: 18 })}<p>${escapeHTML(upload.message || "The upload failed.")}</p>
          <div class="cv-asset-actions">
            ${upload.file ? '<button type="button" class="btn btn-secondary cv-btn-sm" data-cv-action="retry-upload">Try again</button>' : ""}
            <button type="button" class="btn btn-tertiary cv-btn-sm" data-cv-action="pick-image">Choose another</button>
          </div>${input}
        </div>`;
      return;
    }
    if (assetId) {
      record.body.innerHTML = `<div class="cv-asset has-image" data-surface="dark">
          <button type="button" class="cv-asset-view" data-cv-action="open-media" data-cv-asset="${escapeHTML(assetId)}" data-cv-kind="image" aria-label="Open the image larger">
            <span class="cv-skeleton" aria-hidden="true"></span>
          </button>
          ${readOnly ? "" : `<button type="button" class="cv-asset-replace" data-cv-action="pick-image">${icon("upload", { size: 13 })}Replace</button>`}
          ${input}
        </div>`;
      this.hydrateImage(record, assetId, record.body.querySelector(".cv-asset-view"));
      return;
    }
    record.body.innerHTML = readOnly
      ? `<div class="cv-asset is-empty${invalid ? " is-invalid" : ""}">${icon("image", { size: 22 })}<p>No image</p></div>`
      : `<button type="button" class="cv-asset is-empty is-drop${invalid ? " is-invalid" : ""}" data-cv-action="pick-image">
          ${icon("upload", { size: 20 })}
          <strong>Drop an image or click to upload</strong>
          <small>PNG, JPG or WebP, up to 20 MB</small>
        </button>${input}`;
  }

  hydrateImage(record, assetId, host, { className = "" } = {}) {
    if (!host) return;
    this.env.media.thumbnail(assetId).then((media) => {
      if (!host.isConnected) return;
      if (!media) {
        host.innerHTML = `<span class="cv-media-missing">${icon("image", { size: 18 })}<span>Preview unavailable</span></span>`;
        return;
      }
      const image = document.createElement("img");
      image.alt = "";
      image.draggable = false;
      image.decoding = "async";
      if (className) image.className = className;
      image.src = media.url;
      host.replaceChildren(image);
    });
  }

  /* ---------------------------------------------------------------- result */

  renderResult(record) {
    const { node, spec } = record;
    const run = this.env.results.get(node.id) || null;
    const last = this.env.lastOutputs.get(node.id) || null;
    const generate = spec?.category === "generate";
    const status = run?.status || null;
    const showArea = Boolean(spec) && (generate || status === "FAILED" || status === "SKIPPED");
    this.renderHeadStatus(record, run);
    this.paintState(record, run);
    if (!showArea) {
      record.result.hidden = true;
      record.result.innerHTML = "";
      record.resultKey = null;
      return;
    }
    const output = outputOf(spec, (status === "SUCCEEDED" || status === "CACHED") ? run.outputs : last?.outputs);
    const job = run?.job || null;
    const key = JSON.stringify([
      status, run?.id, output?.assetId, output?.text?.length, output?.text?.slice(0, 64),
      run?.error_code, run?.error_message, run?.finished_at, job?.status, this.env.readOnly,
    ]);
    record.result.hidden = false;
    if (key === record.resultKey) {
      this.updateProgress(record, run, job);
      this.syncButtons(record);
      return;
    }
    record.resultKey = key;
    const outputType = spec.outputs[0]?.data_type || "text";
    const running = status === "RUNNING" || status === "PENDING";
    // An output from an earlier run, shown while this node runs again or after it did not finish.
    const stale = Boolean(output) && Boolean(status) && status !== "SUCCEEDED" && status !== "CACHED";
    let content = "";
    if (output?.dataType === "text") {
      content = `<div class="cv-text-out${stale ? " is-stale" : ""}">
          <div class="cv-text-out-body" data-cv-scroll>${escapeHTML(output.text) || '<span class="cv-muted">The model returned no text.</span>'}</div>
          <div class="cv-text-out-actions">
            <button type="button" class="cv-node-btn" data-cv-action="copy-output" title="Copy text" aria-label="Copy output text">${icon("copy", { size: 13 })}</button>
            <button type="button" class="cv-node-btn" data-cv-action="open-text" title="Open larger" aria-label="Open the output larger">${icon("expand", { size: 13 })}</button>
          </div>
        </div>`;
    } else if (output?.assetId && output.dataType === "image") {
      content = `<div class="cv-media is-image${stale ? " is-stale" : ""}" data-surface="dark">
          <button type="button" class="cv-media-view" data-cv-action="open-media" data-cv-asset="${escapeHTML(output.assetId)}" data-cv-kind="image" aria-label="Open the image larger"><span class="cv-skeleton" aria-hidden="true"></span></button>
        </div>`;
    } else if (output?.assetId && output.dataType === "video") {
      content = `<div class="cv-media is-video${stale ? " is-stale" : ""}" data-surface="dark" data-cv-asset="${escapeHTML(output.assetId)}">
          <span class="cv-media-poster"><span class="cv-skeleton" aria-hidden="true"></span></span>
          <button type="button" class="cv-media-play" data-cv-action="play-video" data-cv-asset="${escapeHTML(output.assetId)}" aria-label="Play the video">${icon("play", { size: 18 })}</button>
          <button type="button" class="cv-node-btn cv-media-expand" data-cv-action="open-media" data-cv-asset="${escapeHTML(output.assetId)}" data-cv-kind="video" title="Open larger" aria-label="Open the video larger">${icon("expand", { size: 13 })}</button>
        </div>`;
    } else if (outputType === "text") {
      content = `<div class="cv-text-out is-empty"><span>${running ? "Thinking…" : "The output appears here after a run."}</span></div>`;
    } else {
      content = `<div class="cv-media is-placeholder is-${escapeHTML(outputType)}" data-surface="dark">
          ${icon(outputType === "video" ? "film" : "image", { size: 22 })}
          <span>${running ? (status === "PENDING" ? "Queued" : "Generating…") : `Your ${escapeHTML(outputType)} appears here`}</span>
        </div>`;
    }
    let state = "";
    if (running) {
      state = `<div class="cv-run-line tone-running"><span class="cv-run-label">${status === "PENDING" ? "Queued" : this.runningLabel(run, job)}</span><span class="cv-run-pct mono"></span></div>`;
    } else if (status === "FAILED") {
      const topUp = TOP_UP_CODES.has(run.error_code) || TOP_UP_CODES.has(job?.error_code);
      state = `<div class="cv-run-line tone-failed" role="alert">
          <p>${escapeHTML(run.error_message || job?.error_message || "This node failed.")}</p>
          <div class="cv-run-actions">
            ${topUp ? `<button type="button" class="btn btn-primary cv-btn-sm" data-cv-action="topup">${run.error_code === "PLAN_DENIED" ? "See plans" : "Top up"}</button>` : ""}
            <button type="button" class="btn btn-secondary cv-btn-sm" data-cv-action="retry"${this.env.readOnly || this.env.runActive ? " disabled" : ""}>${icon("refresh", { size: 13 })}Retry</button>
          </div>
        </div>`;
    } else if (status === "SKIPPED") {
      state = '<div class="cv-run-line tone-muted">Skipped – an input failed</div>';
    } else if (status === "CANCELLED") {
      state = '<div class="cv-run-line tone-muted">Cancelled</div>';
    } else if (status === "CACHED") {
      state = `<div class="cv-run-line tone-ok" title="${escapeHTML(RUN_STATUS.CACHED.title)}">${icon("check", { size: 12 })}<span>Reused · no new charge</span></div>`;
    } else if (status === "SUCCEEDED") {
      state = `<div class="cv-run-line tone-ok">${icon("check", { size: 12 })}<span>Done${run.finished_at ? ` · ${escapeHTML(clockTime(run.finished_at))}` : ""}</span></div>`;
    }
    record.result.innerHTML = `${content}${state}`;
    if (output?.assetId && output.dataType === "image") {
      this.hydrateImage(record, output.assetId, record.result.querySelector(".cv-media-view"));
    }
    if (output?.assetId && output.dataType === "video") {
      this.hydrateImage(record, output.assetId, record.result.querySelector(".cv-media-poster"));
    }
    this.updateProgress(record, run, job);
    this.syncButtons(record);
  }

  /** Run and Retry are unavailable while any run is active or the canvas is view only. */
  syncButtons(record) {
    const blocked = this.env.readOnly || this.env.runActive;
    record.head.querySelectorAll(".cv-node-run").forEach((button) => { button.disabled = blocked; });
    record.result.querySelectorAll('[data-cv-action="retry"]').forEach((button) => { button.disabled = blocked; });
  }

  runningLabel(run, job) {
    if (!job) return "Running";
    if (job.status === "QUEUED" || job.status === "SUBMITTED") return "Waiting for the provider";
    if (job.status === "RETRY_WAIT") return "Retrying shortly";
    return "Generating";
  }

  renderHeadStatus(record, run) {
    const current = record.head.querySelector(".cv-status");
    const status = run ? RUN_STATUS[run.status] : null;
    const wanted = status ? `tone-${status.tone}` : null;
    if ((current && wanted && current.classList.contains(wanted) && current.textContent.trim() === status.label)
      || (!current && !status)) {
      return;
    }
    if (record.head.querySelector(".cv-node-rename")) return;
    this.renderHead(record);
  }

  paintState(record, run) {
    const status = run?.status;
    const { el } = record;
    el.classList.toggle("cv-is-running", status === "RUNNING");
    el.classList.toggle("cv-is-queued", status === "PENDING");
    el.classList.toggle("cv-is-failed", status === "FAILED");
    el.classList.toggle("cv-is-done", status === "SUCCEEDED" || status === "CACHED");
    el.classList.toggle("cv-is-skipped", status === "SKIPPED" || status === "CANCELLED");
  }

  updateProgress(record, run, job) {
    const running = run?.status === "RUNNING";
    const progress = running && typeof job?.progress === "number" ? Math.max(0, Math.min(1, job.progress)) : null;
    record.progress.hidden = !running;
    record.progress.classList.toggle("is-indeterminate", running && progress === null);
    const bar = record.progress.querySelector("i");
    bar.style.width = progress === null ? "" : `${Math.round(progress * 100)}%`;
    const pct = record.result.querySelector(".cv-run-pct");
    if (pct) pct.textContent = progress === null ? "" : `${Math.round(progress * 100)}%`;
  }

  /* ---------------------------------------------------------------- errors and footer */

  renderErrors(record, { ports = true } = {}) {
    const issues = this.env.errors.get(record.id) || [];
    const had = record.errorKey || "";
    record.errorKey = issues.map((issue) => `${issue.param || ""}|${issue.port || ""}|${issue.message}`).join("\n");
    record.el.classList.toggle("has-errors", issues.length > 0);
    record.errors.hidden = issues.length === 0;
    record.errors.innerHTML = issues.map((issue) => `<li>${icon("alert", { size: 12 })}<span>${escapeHTML(issue.message)}</span></li>`).join("");
    for (const [key, field] of record.fields) {
      field.setInvalid(issues.find((issue) => issue.param === key)?.message || null);
    }
    // Ports and the image dropzone carry error marks too; redraw them only when the marks changed.
    if (had === record.errorKey) return;
    if (ports && record.spec && record.spec.inputs.length) this.renderPorts(record);
    if (record.bodyKey?.startsWith("asset:")) this.renderBody(record, { force: true });
  }

  renderFoot(record) {
    const summary = runSummary(record.node, record.spec, this.env);
    record.foot.hidden = !summary;
    record.foot.textContent = summary;
    record.foot.title = summary;
  }

  /* ---------------------------------------------------------------- refresh from outside */

  refresh(ids = null) {
    for (const id of ids || this.records.keys()) {
      const record = this.records.get(id);
      if (!record) continue;
      this.renderResult(record);
      this.renderErrors(record);
    }
  }

  /**
   * Connections, platform models or the view-only flag changed: re-render
   * what depends on them. A card the user is typing in keeps its body.
   */
  refreshContext() {
    for (const record of this.records.values()) {
      this.renderHead(record);
      if (!record.body.contains(document.activeElement)) this.renderBody(record, { force: true });
      else this.syncFields(record);
      this.renderFoot(record);
      this.syncButtons(record);
    }
  }

  refreshPorts(ids) {
    for (const id of ids) {
      const record = this.records.get(id);
      if (!record) continue;
      this.renderPorts(record);
      this.renderBody(record);
    }
  }

  /* ---------------------------------------------------------------- events */

  onClick(event) {
    const button = event.target.closest("[data-cv-action]");
    if (!button || !this.layer.contains(button)) return;
    const card = button.closest(".cv-node");
    const id = card?.dataset.cvNode;
    const record = id ? this.records.get(id) : null;
    if (!record) return;
    const action = button.dataset.cvAction;
    const actions = this.env.actions;
    if (action === "run-node" || action === "retry") {
      event.stopPropagation();
      actions.runNodes?.([id]);
    } else if (action === "node-menu") {
      event.stopPropagation();
      this.editor?.openNodeMenu(id, button);
    } else if (action === "topup") {
      actions.topUp?.();
    } else if (action === "pick-image") {
      record.body.querySelector("[data-cv-file]")?.click();
    } else if (action === "retry-upload") {
      const upload = this.env.uploads.get(id);
      if (upload?.file) actions.uploadImage?.(id, upload.file);
    } else if (action === "open-media") {
      actions.openViewer?.({ assetId: button.dataset.cvAsset, kind: button.dataset.cvKind, title: nodeTitle(record.node, record.spec) });
    } else if (action === "copy-output" || action === "open-text") {
      const run = this.env.results.get(id);
      const last = this.env.lastOutputs.get(id);
      const output = outputOf(record.spec, (run?.status === "SUCCEEDED" || run?.status === "CACHED") ? run.outputs : last?.outputs);
      if (!output?.text) return;
      if (action === "copy-output") actions.copyText?.(output.text);
      else actions.openViewer?.({ text: output.text, kind: "text", title: nodeTitle(record.node, record.spec) });
    } else if (action === "play-video") {
      this.playVideo(button.closest(".cv-media"), button.dataset.cvAsset);
    }
  }

  onDoubleClick(event) {
    const title = event.target.closest(".cv-node-title");
    if (!title) return;
    const id = title.closest(".cv-node")?.dataset.cvNode;
    if (id) {
      event.stopPropagation();
      this.startRename(id);
    }
  }

  onFileChange(event) {
    const input = event.target.closest("[data-cv-file]");
    if (!input) return;
    const id = input.closest(".cv-node")?.dataset.cvNode;
    const file = input.files?.[0];
    input.value = "";
    if (id && file) this.env.actions.uploadImage?.(id, file);
  }

  async playVideo(host, assetId) {
    if (!host || !assetId) return;
    const button = host.querySelector(".cv-media-play");
    if (button) {
      button.disabled = true;
      button.innerHTML = '<span class="cv-spinner" aria-hidden="true"></span>';
    }
    const poster = host.querySelector(".cv-media-poster img")?.src || "";
    const media = await this.env.media.media(assetId);
    if (!host.isConnected) return;
    if (!media) {
      if (button) {
        button.disabled = false;
        button.innerHTML = icon("play", { size: 18 });
      }
      this.env.actions.toast?.("The video could not be loaded.");
      return;
    }
    const video = document.createElement("video");
    video.controls = true;
    video.playsInline = true;
    video.preload = "auto";
    if (poster) video.poster = poster;
    video.src = media.url;
    video.dataset.cvNodrag = "";
    host.querySelector(".cv-media-poster")?.remove();
    button?.remove();
    host.prepend(video);
    video.play().catch(() => null);
  }
}
