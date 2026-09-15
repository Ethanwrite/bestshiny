/**
 * The right-hand panel. With one node selected: its title, every visible
 * parameter, its inputs (and a keyboard way to connect them), and its latest
 * result with usage and billing. With several: bulk actions. With an edge:
 * what it joins. With nothing: the canvas itself and its recent runs.
 */
import { effectiveData, inputPort, outputPort, paramVisible } from "./graph.js";
import { createField } from "./fields.js";
import { icon, hasIcon } from "./icons.js";
import { RUN_STATUS, dataTypeClass, nodeTitle } from "./node-card.js";
import { MOD, clockTime, escapeHTML, friendlyModel, relativeTime } from "./ui.js";

const RUN_STATE = {
  QUEUED: { label: "Queued", tone: "queued" },
  RUNNING: { label: "Running", tone: "running" },
  SUCCEEDED: { label: "Finished", tone: "ok" },
  FAILED: { label: "Failed", tone: "danger" },
  CANCELLED: { label: "Stopped", tone: "neutral" },
};

const JOB_STATUS = {
  QUEUED: "Queued", SUBMITTED: "Submitted", RUNNING: "Generating", RETRY_WAIT: "Waiting to retry",
  COMPLETED: "Completed", FAILED: "Failed", CANCELLED: "Cancelled", WORKER_NEEDS_USER_ACTION: "Needs your action",
};

function statusChip(view) {
  if (!view) return "";
  return `<span class="cv-status tone-${view.tone}"${view.title ? ` title="${escapeHTML(view.title)}"` : ""}>${view.icon ? icon(view.icon, { size: 12 }) : "<i></i>"}<span>${escapeHTML(view.label)}</span></span>`;
}

export class Inspector {
  constructor(host, env) {
    this.host = host;
    this.env = env;
    this.selection = { nodeIds: [], edgeId: null };
    this.fields = new Map();
    this.structure = null;
    host.innerHTML = '<div class="cv-inspector-scroll"></div>';
    this.scroll = host.querySelector(".cv-inspector-scroll");
    host.addEventListener("click", (event) => this.onClick(event));
    host.addEventListener("change", (event) => this.onChange(event));
  }

  get store() {
    return this.env.store;
  }

  setSelection(selection) {
    this.selection = { nodeIds: selection?.nodeIds || [], edgeId: selection?.edgeId || null };
    this.render({ keepScroll: false });
  }

  selectedNode() {
    return this.selection.nodeIds.length === 1 ? this.store?.getNode(this.selection.nodeIds[0]) : null;
  }

  /* ---------------------------------------------------------------- store events */

  onStoreChange(change) {
    if (!this.store) return;
    const node = this.selectedNode();
    if (change.kind === "viewport") return;
    if (change.kind === "replace" || change.kind === "remove") {
      this.render();
      return;
    }
    if (!node) {
      if (!this.selection.nodeIds.length && !this.selection.edgeId) this.updateWorkflowFacts();
      else if (this.selection.edgeId && change.kind === "edges") this.render();
      else if (this.selection.nodeIds.length > 1 && change.kind === "label") this.render();
      return;
    }
    const touches = (change.nodeIds || []).includes(node.id);
    if (change.kind === "data" && touches) this.syncNode(node);
    else if (change.kind === "label" && touches) this.syncTitle(node);
    else if (change.kind === "edges" && touches) this.renderPortsSection(node);
    else if (change.kind === "label") this.renderPortsSection(node);
  }

  refreshResults(ids = null) {
    const node = this.selectedNode();
    if (node && (!ids || ids.includes(node.id))) {
      this.renderResultSection(node);
      const errors = this.env.errors.get(node.id) || [];
      for (const [key, field] of this.fields) field.setInvalid(errors.find((issue) => issue.param === key)?.message || null);
    }
    if (!this.selection.nodeIds.length && !this.selection.edgeId) this.renderRuns();
    this.syncRunButtons();
  }

  /** Validation issues changed for some nodes: mark the selected node's fields and inputs. */
  refreshErrors(ids = null) {
    const node = this.selectedNode();
    if (!node || (ids && !ids.includes(node.id))) return;
    const errors = this.env.errors.get(node.id) || [];
    for (const [key, field] of this.fields) field.setInvalid(errors.find((issue) => issue.param === key)?.message || null);
    const asset = this.scroll.querySelector(".cv-insp-asset");
    if (asset) this.renderSettings(node);
    this.renderPortsSection(node);
  }

  refreshContext() {
    const node = this.selectedNode();
    if (node && this.scroll.contains(document.activeElement)) {
      // Keep the field the user is in; refresh the pickers around it.
      this.syncNode(node);
      this.syncRunButtons();
      return;
    }
    this.render();
  }

  /** Run buttons follow the canvas's run state without re-rendering the panel. */
  syncRunButtons() {
    const blocked = this.env.readOnly || this.env.runActive;
    this.scroll.querySelectorAll('[data-cv-insp="run"], [data-cv-insp="run-many"]').forEach((button) => {
      button.disabled = blocked || (button.dataset.cvInsp === "run-many" && !this.selection.nodeIds.some((id) => this.store?.catalog.get(this.store.getNode(id)?.type)?.executable));
    });
  }

  /* ---------------------------------------------------------------- render */

  render({ keepScroll = true } = {}) {
    const scrollTop = this.scroll.scrollTop;
    this.fields.clear();
    this.structure = null;
    if (!this.store || !this.env.catalog) {
      this.scroll.innerHTML = '<div class="cv-panel-loading"><span class="cv-spinner" aria-hidden="true"></span>Loading…</div>';
      return;
    }
    const { nodeIds, edgeId } = this.selection;
    if (nodeIds.length === 1 && this.store.getNode(nodeIds[0])) this.renderNode(this.store.getNode(nodeIds[0]));
    else if (nodeIds.length > 1) this.renderMany(nodeIds.filter((id) => this.store.getNode(id)));
    else if (edgeId && this.store.getEdge(edgeId)) this.renderEdge(this.store.getEdge(edgeId));
    else this.renderWorkflow();
    this.scroll.scrollTop = keepScroll ? scrollTop : 0;
  }

  /* ---- one node ---- */

  renderNode(node) {
    const spec = this.store.catalog.get(node.type);
    const readOnly = this.env.readOnly;
    const title = nodeTitle(node, spec);
    this.scroll.innerHTML = `
      <header class="cv-insp-head">
        <span class="cv-node-icon c-${escapeHTML(spec?.category || "unknown")}">${icon(spec && hasIcon(spec.icon) ? spec.icon : "alert", { size: 16 })}</span>
        <div class="cv-insp-titles">
          <input class="cv-insp-title" type="text" maxlength="120" value="${escapeHTML(node.label || "")}" placeholder="${escapeHTML(spec?.title || title)}" aria-label="Node title"${readOnly ? " disabled" : ""} />
          <span class="cv-insp-type">${escapeHTML(spec?.title || `Unknown type ${node.type}`)}</span>
        </div>
      </header>
      ${spec?.description ? `<p class="cv-insp-desc">${escapeHTML(spec.description)}</p>` : ""}
      <section class="cv-insp-section" data-cv-section="settings"></section>
      <section class="cv-insp-section" data-cv-section="ports"></section>
      <section class="cv-insp-section" data-cv-section="result"></section>
      <div class="cv-insp-actions">
        ${spec?.executable ? `<button type="button" class="btn btn-secondary" data-cv-insp="run"${readOnly || this.env.runActive ? " disabled" : ""}>${icon("play", { size: 13 })}Run this node</button>` : ""}
        <button type="button" class="btn btn-tertiary" data-cv-insp="duplicate"${readOnly ? " disabled" : ""}>${icon("duplicate", { size: 14 })}Duplicate</button>
        <button type="button" class="btn btn-danger" data-cv-insp="delete"${readOnly ? " disabled" : ""}>${icon("trash", { size: 14 })}Delete</button>
      </div>`;
    const titleInput = this.scroll.querySelector(".cv-insp-title");
    titleInput.addEventListener("change", () => this.store.renameNode(node.id, titleInput.value));
    titleInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") titleInput.blur();
    });
    this.renderSettings(node);
    this.renderPortsSection(node);
    this.renderResultSection(node);
  }

  settingsStructure(node, spec) {
    const data = effectiveData(spec, node.data);
    const visible = spec.params.filter((param) => paramVisible(param, data));
    const upload = this.env.uploads.get(node.id);
    return JSON.stringify([
      node.id,
      visible.map((param) => `${param.key}${spec.inputs.some((port) => port.key === param.key) && this.store.incoming(node.id, param.key).length ? "+" : ""}`),
      this.env.readOnly,
      visible.some((param) => param.kind === "asset") ? [node.data.asset_id, upload?.status, upload?.message] : null,
    ]);
  }

  renderSettings(node) {
    const section = this.scroll.querySelector('[data-cv-section="settings"]');
    const spec = this.store.catalog.get(node.type);
    if (!section) return;
    this.fields.clear();
    if (!spec) {
      section.innerHTML = '<p class="cv-insp-note">This node type is no longer offered. It keeps the settings it was saved with.</p>';
      return;
    }
    this.structure = this.settingsStructure(node, spec);
    const data = effectiveData(spec, node.data);
    const visible = spec.params.filter((param) => paramVisible(param, data));
    const errors = this.env.errors.get(node.id) || [];
    section.innerHTML = `<h3 class="eyebrow">Settings</h3>${visible.length ? "" : '<p class="cv-insp-note">This node has no settings.</p>'}`;
    for (const param of visible) {
      if (param.kind === "asset") {
        section.append(this.assetField(node, param, errors.find((issue) => issue.param === param.key)));
        continue;
      }
      const connected = spec.inputs.some((port) => port.key === param.key) && this.store.incoming(node.id, param.key).length > 0;
      const field = createField(param, {
        compact: false,
        value: node.data[param.key] ?? data[param.key],
        data,
        disabled: this.env.readOnly,
        connected,
        connectedFrom: connected ? this.sourceTitle(node.id, param.key) : null,
        env: this.env,
        onChange: (key, value, options) => this.store.updateNodeData(node.id, { [key]: value }, options),
      });
      field.setInvalid(errors.find((issue) => issue.param === param.key)?.message || null);
      this.fields.set(param.key, field);
      section.append(field.el);
    }
  }

  assetField(node, param, issue) {
    const wrapper = document.createElement("div");
    wrapper.className = `cv-field cv-insp-asset${issue ? " is-invalid" : ""}`;
    const upload = this.env.uploads.get(node.id);
    const assetId = node.data.asset_id;
    const readOnly = this.env.readOnly;
    wrapper.innerHTML = `
      <span class="cv-field-label">${escapeHTML(param.label)}${param.required ? '<span class="cv-required" aria-hidden="true">*</span>' : ""}</span>
      <div class="cv-insp-asset-box" data-surface="dark">
        ${upload?.status === "uploading"
          ? `<span class="cv-spinner" aria-hidden="true"></span><span>Uploading ${escapeHTML(upload.name || "image")}…</span>`
          : assetId ? '<button type="button" class="cv-insp-asset-view" data-cv-insp="view-asset" aria-label="Open the image larger"><span class="cv-skeleton" aria-hidden="true"></span></button>'
            : `<span class="cv-insp-asset-empty">${icon("image", { size: 20 })}<span>No image yet</span></span>`}
      </div>
      ${upload?.status === "error" ? `<p class="cv-field-error">${escapeHTML(upload.message || "The upload failed.")}</p>` : ""}
      ${issue ? `<p class="cv-field-error">${escapeHTML(issue.message)}</p>` : ""}
      ${readOnly ? "" : `<div class="btn-row"><button type="button" class="btn btn-secondary" data-cv-insp="pick-asset"${upload?.status === "uploading" ? " disabled" : ""}>${icon("upload", { size: 14 })}${assetId ? "Replace image" : "Upload image"}</button></div>
      <input type="file" accept="image/png,image/jpeg,image/webp" data-cv-insp-file hidden />`}
      <p class="cv-field-help">PNG, JPG or WebP, up to 20 MB. You can also drop an image on the node.</p>`;
    if (assetId && upload?.status !== "uploading") {
      const host = wrapper.querySelector(".cv-insp-asset-view");
      this.env.media.thumbnail(assetId).then((media) => {
        if (!host?.isConnected) return;
        if (!media) {
          host.innerHTML = `<span class="cv-media-missing">${icon("image", { size: 18 })}<span>Preview unavailable</span></span>`;
          return;
        }
        const image = document.createElement("img");
        image.alt = "";
        image.src = media.url;
        host.replaceChildren(image);
      });
    }
    return wrapper;
  }

  syncNode(node) {
    const spec = this.store.catalog.get(node.type);
    if (!spec) return;
    if (this.settingsStructure(node, spec) !== this.structure) {
      const section = this.scroll.querySelector('[data-cv-section="settings"]');
      const focusedKey = document.activeElement?.closest?.("[data-cv-param]")?.dataset.cvParam;
      this.renderSettings(node);
      if (focusedKey) section?.querySelector(`[data-cv-param="${CSS.escape(focusedKey)}"] input, [data-cv-param="${CSS.escape(focusedKey)}"] select, [data-cv-param="${CSS.escape(focusedKey)}"] textarea`)?.focus();
    } else {
      const data = effectiveData(spec, node.data);
      const errors = this.env.errors.get(node.id) || [];
      for (const [key, field] of this.fields) {
        const connected = spec.inputs.some((port) => port.key === key) && this.store.incoming(node.id, key).length > 0;
        field.sync(node.data[key] ?? data[key], {
          data, disabled: this.env.readOnly, connected,
          connectedFrom: connected ? this.sourceTitle(node.id, key) : null, env: this.env,
        });
        field.setInvalid(errors.find((issue) => issue.param === key)?.message || null);
      }
    }
  }

  syncTitle(node) {
    const input = this.scroll.querySelector(".cv-insp-title");
    if (input && document.activeElement !== input) input.value = node.label || "";
  }

  sourceTitle(nodeId, port) {
    const edge = this.store.incoming(nodeId, port)[0];
    const source = edge ? this.store.getNode(edge.source) : null;
    return source ? nodeTitle(source, this.store.catalog.get(source.type)) : null;
  }

  renderPortsSection(node) {
    const section = this.scroll.querySelector('[data-cv-section="ports"]');
    const spec = this.store.catalog.get(node.type);
    if (!section) return;
    if (!spec?.inputs.length) {
      section.hidden = true;
      section.innerHTML = "";
      return;
    }
    section.hidden = false;
    const errors = this.env.errors.get(node.id) || [];
    const readOnly = this.env.readOnly;
    const typeLabel = (dataType) => this.store.catalog.dataTypes?.[dataType]?.label || dataType;
    const rows = spec.inputs.map((port) => {
      const edges = this.store.incoming(node.id, port.key);
      const links = edges.map((edge) => {
        const source = this.store.getNode(edge.source);
        const sourceSpec = this.store.catalog.get(source?.type);
        const output = outputPort(sourceSpec, edge.source_port);
        return `<li><span>From <b>${escapeHTML(nodeTitle(source, sourceSpec))}</b>${sourceSpec && sourceSpec.outputs.length > 1 ? ` · ${escapeHTML(output?.label || edge.source_port)}` : ""}</span>
          ${readOnly ? "" : `<button type="button" class="cv-icon-btn" data-cv-insp="disconnect" data-cv-edge-id="${escapeHTML(edge.id)}" aria-label="Disconnect ${escapeHTML(port.label)} from ${escapeHTML(nodeTitle(source, sourceSpec))}" title="Disconnect">${icon("x", { size: 12 })}</button>`}</li>`;
      }).join("");
      const capacity = port.multiple ? port.max_connections : 1;
      const options = readOnly ? [] : this.connectOptions(node, port);
      const issue = errors.find((item) => item.port === port.key);
      const canAddMore = edges.length < capacity || !port.multiple;
      return `<li class="cv-insp-port ${dataTypeClass(port.data_type)}${issue ? " is-invalid" : ""}">
          <span class="cv-port-dot${edges.length ? " is-connected" : ""}" aria-hidden="true"></span>
          <div class="cv-insp-port-main">
            <div class="cv-insp-port-name"><strong>${escapeHTML(port.label)}</strong><small>${escapeHTML(typeLabel(port.data_type))}${port.multiple ? ` · up to ${port.max_connections}` : ""}${port.required ? " · required" : ""}</small></div>
            ${links ? `<ul class="cv-insp-links">${links}</ul>` : '<p class="cv-insp-unlinked">Not connected</p>'}
            ${issue ? `<p class="cv-field-error">${escapeHTML(issue.message)}</p>` : ""}
            ${!readOnly && options.length && canAddMore ? `<select class="cv-insp-connect" data-cv-connect-port="${escapeHTML(port.key)}" aria-label="Connect ${escapeHTML(port.label)} from another node">
                <option value="">${edges.length && !port.multiple ? "Replace with…" : "Connect from…"}</option>
                ${options.map((option) => `<option value="${escapeHTML(option.value)}">${escapeHTML(option.label)}</option>`).join("")}
              </select>` : ""}
          </div>
        </li>`;
    }).join("");
    section.innerHTML = `<h3 class="eyebrow">Inputs</h3><ul class="cv-insp-ports">${rows}</ul>`;
  }

  connectOptions(node, port) {
    const options = [];
    for (const other of this.store.nodes) {
      if (other.id === node.id) continue;
      const spec = this.store.catalog.get(other.type);
      for (const output of spec?.outputs || []) {
        if (output.data_type !== port.data_type) continue;
        const verdict = this.store.canConnect({ source: other.id, source_port: output.key, target: node.id, target_port: port.key });
        if (!verdict.ok) continue;
        options.push({
          value: `${other.id}|${output.key}`,
          label: `${nodeTitle(other, spec)}${spec.outputs.length > 1 ? ` · ${output.label}` : ""}`,
        });
      }
    }
    return options;
  }

  renderResultSection(node) {
    const section = this.scroll.querySelector('[data-cv-section="result"]');
    const spec = this.store.catalog.get(node.type);
    if (!section) return;
    if (!spec?.executable) {
      section.hidden = true;
      section.innerHTML = "";
      return;
    }
    section.hidden = false;
    const run = this.env.results.get(node.id);
    if (!run) {
      section.innerHTML = `<h3 class="eyebrow">Latest result</h3><p class="cv-insp-note">Not run yet. Run this node, or the whole canvas, to see its result here.</p>`;
      return;
    }
    const status = RUN_STATUS[run.status] || { label: run.status, tone: "neutral" };
    const job = run.job;
    const usage = run.usage || {};
    const facts = [];
    if (run.finished_at) facts.push(["Finished", `${clockTime(run.finished_at)} · ${relativeTime(run.finished_at)}`]);
    else if (run.started_at) facts.push(["Started", `${clockTime(run.started_at)} · ${relativeTime(run.started_at)}`]);
    if (usage.served_model) facts.push(["Served by", usage.served_model]);
    const tokens = [usage.input_tokens, usage.output_tokens].some((value) => Number.isFinite(Number(value)) && value !== null && value !== undefined);
    if (tokens) facts.push(["Tokens", `${Number(usage.input_tokens || 0).toLocaleString()} in · ${Number(usage.output_tokens || 0).toLocaleString()} out`]);
    if (job) {
      const own = job.billing_owner === "USER_CONNECTION";
      facts.push(["Job", `${JOB_STATUS[job.status] || job.status}${typeof job.progress === "number" && job.status !== "COMPLETED" ? ` · ${Math.round(job.progress * 100)}%` : ""}`]);
      if (job.model) facts.push(["Model", own ? job.model : friendlyModel(job.model)]);
      const connection = (this.env.connections || []).find((item) => item.id === node.data.connection_id);
      const billedTo = own
        ? `your ${connection ? `“${connection.name}”` : "own"} account at the provider`
        : `BestShiny credits${Number(job.quoted_credits) > 0 ? ` (≈${Number(job.quoted_credits).toLocaleString()})` : ""}`;
      // A reused result points at the job that made it; that job was paid for once, not again.
      facts.push(["Billing", run.status === "CACHED" ? `No new charge — first billed to ${billedTo}` : `Billed to ${billedTo}`]);
      if (job.deleted) facts.push(["Note", "The media was removed from Productions."]);
    }
    if (run.status === "CACHED" || usage.cached) {
      facts.push(["Reuse", job ? "An earlier result for exactly these inputs" : "An earlier result for exactly these inputs — no new charge"]);
    }
    const output = this.outputPreview(spec, run);
    section.innerHTML = `
      <h3 class="eyebrow">Latest result</h3>
      <div class="cv-insp-status">${statusChip(status)}</div>
      ${output}
      ${run.status === "FAILED" ? `<div class="cv-insp-error" role="alert"><strong>${escapeHTML(run.error_message || job?.error_message || "This node failed.")}</strong>${run.error_code ? `<small class="mono">${escapeHTML(run.error_code)}</small>` : ""}</div>` : ""}
      ${run.status === "SKIPPED" ? '<p class="cv-insp-note">Skipped – an input failed.</p>' : ""}
      ${facts.length ? `<dl class="cv-insp-facts">${facts.map(([term, value]) => `<dt>${escapeHTML(term)}</dt><dd>${escapeHTML(value)}</dd>`).join("")}</dl>` : ""}
      ${run.status === "FAILED" && ["INSUFFICIENT_CREDITS", "PLAN_DENIED"].includes(run.error_code) ? `<button type="button" class="btn btn-primary" data-cv-insp="topup">${run.error_code === "PLAN_DENIED" ? "See plans" : "Top up credits"}</button>` : ""}`;
    const thumb = section.querySelector("[data-cv-insp-thumb]");
    if (thumb) {
      this.env.media.thumbnail(thumb.dataset.cvInspThumb).then((media) => {
        if (!thumb.isConnected || !media) return;
        const image = document.createElement("img");
        image.alt = "";
        image.src = media.url;
        thumb.replaceChildren(image);
      });
    }
  }

  outputPreview(spec, run) {
    if (!["SUCCEEDED", "CACHED"].includes(run.status)) return "";
    for (const port of spec.outputs) {
      const value = run.outputs?.[port.key];
      if (value === undefined || value === null) continue;
      if (port.data_type === "text") {
        return `<div class="cv-insp-text"><pre data-cv-scroll>${escapeHTML(String(value))}</pre>
          <div class="btn-row"><button type="button" class="btn btn-tertiary" data-cv-insp="copy-output">${icon("copy", { size: 13 })}Copy</button><button type="button" class="btn btn-tertiary" data-cv-insp="open-output">${icon("expand", { size: 13 })}Open</button></div></div>`;
      }
      if (value?.asset_id) {
        return `<button type="button" class="cv-insp-media" data-surface="dark" data-cv-insp="view-output" data-cv-asset="${escapeHTML(value.asset_id)}" data-cv-kind="${escapeHTML(port.data_type)}" aria-label="Open the ${escapeHTML(port.data_type)} larger">
            <span class="cv-insp-thumb" data-cv-insp-thumb="${escapeHTML(value.asset_id)}"><span class="cv-skeleton" aria-hidden="true"></span></span>
            ${port.data_type === "video" ? `<span class="cv-insp-play">${icon("play", { size: 16 })}</span>` : ""}
          </button>`;
      }
    }
    return "";
  }

  /* ---- several nodes ---- */

  renderMany(ids) {
    const readOnly = this.env.readOnly;
    const nodes = ids.map((id) => this.store.getNode(id));
    const runnable = nodes.filter((node) => this.store.catalog.get(node.type)?.executable);
    this.scroll.innerHTML = `
      <header class="cv-insp-head"><span class="cv-node-icon">${icon("grid", { size: 16 })}</span><div class="cv-insp-titles"><h2 class="cv-insp-heading">${nodes.length} nodes selected</h2><span class="cv-insp-type">Shift-click to add or remove</span></div></header>
      <ul class="cv-insp-list">${nodes.map((node) => {
        const spec = this.store.catalog.get(node.type);
        return `<li><button type="button" class="cv-insp-list-item" data-cv-insp="focus-node" data-cv-node-id="${escapeHTML(node.id)}"><span class="cv-node-icon c-${escapeHTML(spec?.category || "unknown")}">${icon(spec && hasIcon(spec.icon) ? spec.icon : "square", { size: 14 })}</span><span>${escapeHTML(nodeTitle(node, spec))}</span></button></li>`;
      }).join("")}</ul>
      <div class="cv-insp-actions">
        <button type="button" class="btn btn-secondary" data-cv-insp="run-many"${readOnly || !runnable.length || this.env.runActive ? " disabled" : ""}>${icon("play", { size: 13 })}Run ${runnable.length} selected</button>
        <button type="button" class="btn btn-tertiary" data-cv-insp="duplicate-many"${readOnly ? " disabled" : ""}>${icon("duplicate", { size: 14 })}Duplicate</button>
        <button type="button" class="btn btn-danger" data-cv-insp="delete-many"${readOnly ? " disabled" : ""}>${icon("trash", { size: 14 })}Delete</button>
      </div>`;
  }

  /* ---- an edge ---- */

  renderEdge(edge) {
    const source = this.store.getNode(edge.source);
    const target = this.store.getNode(edge.target);
    const sourceSpec = this.store.catalog.get(source?.type);
    const targetSpec = this.store.catalog.get(target?.type);
    const output = outputPort(sourceSpec, edge.source_port);
    const input = inputPort(targetSpec, edge.target_port);
    const typeLabel = this.store.catalog.dataTypes?.[output?.data_type]?.label || output?.data_type || "";
    this.scroll.innerHTML = `
      <header class="cv-insp-head"><span class="cv-node-icon">${icon("link", { size: 16 })}</span><div class="cv-insp-titles"><h2 class="cv-insp-heading">Connection</h2><span class="cv-insp-type">${escapeHTML(typeLabel)}</span></div></header>
      <ol class="cv-insp-edge ${dataTypeClass(output?.data_type)}">
        <li><small>From</small><strong>${escapeHTML(nodeTitle(source, sourceSpec))}</strong><span>${escapeHTML(output?.label || edge.source_port)}</span></li>
        <li><small>To</small><strong>${escapeHTML(nodeTitle(target, targetSpec))}</strong><span>${escapeHTML(input?.label || edge.target_port)}</span></li>
      </ol>
      <div class="cv-insp-actions">
        <button type="button" class="btn btn-danger" data-cv-insp="delete-edge"${this.env.readOnly ? " disabled" : ""}>${icon("trash", { size: 14 })}Delete connection</button>
      </div>`;
  }

  /* ---- the canvas ---- */

  renderWorkflow() {
    const workflow = this.env.workflow;
    if (!workflow) {
      this.scroll.innerHTML = '<p class="cv-insp-note">Open a canvas to see its details.</p>';
      return;
    }
    const readOnly = this.env.readOnly;
    this.scroll.innerHTML = `
      <header class="cv-insp-head"><span class="cv-node-icon">${icon("grid", { size: 16 })}</span><div class="cv-insp-titles"><h2 class="cv-insp-heading">Canvas</h2><span class="cv-insp-type">Nothing selected</span></div></header>
      <div class="cv-field"><label class="cv-field-label" for="cv-insp-workflow-name">Name</label>
        <input id="cv-insp-workflow-name" type="text" maxlength="200" value="${escapeHTML(workflow.name || "")}"${readOnly ? " disabled" : ""} /></div>
      <dl class="cv-insp-facts" data-cv-facts></dl>
      ${readOnly ? '<p class="cv-insp-banner">You can view this canvas. Editing and running need editor access to the workspace.</p>' : ""}
      <section class="cv-insp-section"><h3 class="eyebrow">Recent runs</h3><div data-cv-runs></div></section>
      <section class="cv-insp-section cv-howto">
        <h3 class="eyebrow">How it works</h3>
        <ul>
          <li>${icon("cursor", { size: 14 })}<span>Drag nodes in from the left. Connect an output to an input of the same colour: <b class="t-text">text</b>, <b class="t-image">image</b> or <b class="t-video">video</b>.</span></li>
          <li>${icon("play", { size: 14 })}<span>Run everything, or ▶ one node. Nodes whose inputs have not changed reuse their earlier result instead of paying again.</span></li>
          <li>${icon("plug", { size: 14 })}<span>Add your own model keys in API connections, then set a node's <b>Run on</b> to <b>My API connection</b>.</span></li>
          <li>${icon("keyboard", { size: 14 })}<span>${MOD}+Enter runs, ${MOD}+Z undoes, Space+drag pans, ${MOD}+scroll zooms, F fits the view.</span></li>
        </ul>
      </section>`;
    const input = this.scroll.querySelector("#cv-insp-workflow-name");
    input?.addEventListener("change", () => this.env.actions.renameWorkflow?.(input.value));
    input?.addEventListener("keydown", (event) => { if (event.key === "Enter") input.blur(); });
    this.updateWorkflowFacts();
    this.renderRuns();
  }

  updateWorkflowFacts() {
    const facts = this.scroll.querySelector("[data-cv-facts]");
    const workflow = this.env.workflow;
    if (!facts || !workflow || !this.store) return;
    const input = this.scroll.querySelector("#cv-insp-workflow-name");
    if (input && document.activeElement !== input && input.value !== (workflow.name || "")) input.value = workflow.name || "";
    facts.innerHTML = `
      <dt>Nodes</dt><dd>${this.store.nodes.length}</dd>
      <dt>Connections</dt><dd>${this.store.edges.length}</dd>
      <dt>Saved</dt><dd>Version ${escapeHTML(String(workflow.version || 1))}${workflow.updated_at ? ` · ${escapeHTML(relativeTime(workflow.updated_at))}` : ""}</dd>`;
  }

  renderRuns() {
    const host = this.scroll.querySelector("[data-cv-runs]");
    if (!host) return;
    const runs = this.env.runs;
    if (runs === null) {
      host.innerHTML = '<div class="cv-panel-loading"><span class="cv-spinner" aria-hidden="true"></span>Loading runs…</div>';
      return;
    }
    if (!runs?.length) {
      host.innerHTML = '<p class="cv-insp-note">No runs yet. Press Run to generate everything on the canvas.</p>';
      return;
    }
    host.innerHTML = `<ol class="cv-runs">${runs.slice(0, 20).map((run) => {
      const state = RUN_STATE[run.status] || { label: run.status, tone: "neutral" };
      // The canvas has changed since older runs, so the count is said as it was, not compared to today's.
      const count = run.scope_node_ids?.length || 0;
      const scope = count ? `${count} node${count === 1 ? "" : "s"}` : "";
      return `<li class="cv-runs-item">
          ${statusChip(state)}
          <span class="cv-run-when" title="${escapeHTML(run.created_at || "")}">${escapeHTML(relativeTime(run.finished_at || run.created_at))}</span>
          <span class="cv-run-scope">${escapeHTML(scope)}</span>
          ${run.error_message ? `<p class="cv-run-error">${escapeHTML(run.error_message)}</p>` : ""}
        </li>`;
    }).join("")}</ol>`;
  }

  /* ---------------------------------------------------------------- events */

  onClick(event) {
    const button = event.target.closest("[data-cv-insp]");
    if (!button || !this.store) return;
    const action = button.dataset.cvInsp;
    const actions = this.env.actions;
    const node = this.selectedNode();
    if (action === "run" && node) actions.runNodes?.([node.id]);
    else if (action === "duplicate" && node) actions.duplicate?.([node.id]);
    else if (action === "delete" && node) this.store.removeNodes([node.id]);
    else if (action === "run-many") actions.runNodes?.(this.selection.nodeIds.filter((id) => this.store.catalog.get(this.store.getNode(id)?.type)?.executable));
    else if (action === "duplicate-many") actions.duplicate?.(this.selection.nodeIds);
    else if (action === "delete-many") this.store.removeNodes(this.selection.nodeIds);
    else if (action === "delete-edge" && this.selection.edgeId) this.store.removeEdges([this.selection.edgeId]);
    else if (action === "focus-node") actions.revealNode?.(button.dataset.cvNodeId);
    else if (action === "disconnect") this.store.removeEdges([button.dataset.cvEdgeId]);
    else if (action === "pick-asset") this.scroll.querySelector("[data-cv-insp-file]")?.click();
    else if (action === "view-asset" && node) actions.openViewer?.({ assetId: node.data.asset_id, kind: "image", title: nodeTitle(node, this.store.catalog.get(node.type)) });
    else if (action === "view-output" && node) actions.openViewer?.({ assetId: button.dataset.cvAsset, kind: button.dataset.cvKind, title: nodeTitle(node, this.store.catalog.get(node.type)) });
    else if ((action === "copy-output" || action === "open-output") && node) {
      const run = this.env.results.get(node.id);
      const spec = this.store.catalog.get(node.type);
      const text = spec?.outputs.map((port) => run?.outputs?.[port.key]).find((value) => typeof value === "string");
      if (typeof text !== "string") return;
      if (action === "copy-output") actions.copyText?.(text);
      else actions.openViewer?.({ kind: "text", text, title: nodeTitle(node, spec) });
    } else if (action === "topup") actions.topUp?.();
  }

  onChange(event) {
    const node = this.selectedNode();
    if (!node || !this.store) return;
    const connect = event.target.closest("[data-cv-connect-port]");
    if (connect) {
      const [source, sourcePort] = connect.value.split("|");
      if (!source) return;
      const result = this.store.connect({ source, source_port: sourcePort, target: node.id, target_port: connect.dataset.cvConnectPort });
      if (!result.ok) this.env.actions.toast?.(result.message);
      return;
    }
    const file = event.target.closest("[data-cv-insp-file]");
    if (file) {
      const chosen = file.files?.[0];
      file.value = "";
      if (chosen) this.env.actions.uploadImage?.(node.id, chosen);
    }
  }
}
