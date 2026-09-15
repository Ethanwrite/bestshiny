/**
 * "API connections": a workspace's own provider keys.
 *
 * Owners and admins add, test, edit and remove connections; everyone else
 * sees the list read-only. A key is typed once, sent once and never shown
 * again - the server answers with a masked hint, and this dialog drops the
 * typed key from memory as soon as it leaves the form.
 */
import { api } from "./api.js";
import { icon } from "./icons.js";
import { escapeHTML, relativeTime } from "./ui.js";

const CAPABILITY_LABEL = { chat: "Chat", image: "Image", video: "Video" };
const capabilityLabel = (capability) => CAPABILITY_LABEL[capability] || capability;
const COPY = "Keys are encrypted and never shown again. Generations on your own key are billed by that provider, not by BestShiny credits.";

function chips(capabilities) {
  return `<span class="cv-chips">${(capabilities || []).map((capability) => `<span class="cv-chip cap-${escapeHTML(capability)}">${escapeHTML(capabilityLabel(capability))}</span>`).join("")}</span>`;
}

export class ConnectionsDialog {
  constructor({ onChanged, toast }) {
    this.onChanged = onChanged;
    this.toast = toast;
    this.protocols = null;
    this.state = this.freshState();
    this.dialog = document.createElement("dialog");
    this.dialog.className = "sheet sheet-wide cv-sheet cv-conn-dialog";
    this.dialog.setAttribute("aria-labelledby", "cv-conn-title");
    this.dialog.innerHTML = '<div class="sheet-body cv-conn-body"></div>';
    this.body = this.dialog.querySelector(".cv-conn-body");
    document.body.append(this.dialog);
    this.dialog.addEventListener("click", (event) => {
      if (event.target === this.dialog && !this.state.busy) this.close();
      else this.onClick(event);
    });
    this.dialog.addEventListener("input", (event) => this.onInput(event));
    this.dialog.addEventListener("change", (event) => this.onChange(event));
    this.dialog.addEventListener("submit", (event) => this.onSubmit(event));
    this.dialog.addEventListener("keydown", (event) => this.onKeyDown(event));
    this.dialog.addEventListener("cancel", (event) => {
      if (this.state.busy) event.preventDefault();
    });
    this.dialog.addEventListener("close", () => {
      // The typed key must not outlive the dialog.
      this.state = this.freshState();
      this.body.innerHTML = "";
      this.returnFocus?.focus?.({ preventScroll: true });
    });
  }

  freshState() {
    return {
      workspaceId: null,
      capability: null,
      loading: false,
      loadError: "",
      connections: [],
      canManage: false,
      view: "list",
      form: null,
      busy: false,
      error: "",
      tests: new Map(),
      testing: new Set(),
      confirmDelete: null,
      notice: "",
      remote: null,
    };
  }

  get isOpen() {
    return this.dialog.open;
  }

  async open({ workspaceId, capability = null, add = false } = {}) {
    if (!workspaceId) {
      this.toast?.("Open a project first.");
      return;
    }
    this.returnFocus = document.activeElement;
    this.state = { ...this.freshState(), workspaceId, capability, loading: true };
    this.render();
    if (!this.dialog.open) this.dialog.showModal();
    try {
      const [protocols, list] = await Promise.all([
        this.protocols ? Promise.resolve({ protocols: this.protocols }) : api.protocols(),
        api.connections(workspaceId),
      ]);
      if (this.state.workspaceId !== workspaceId || !this.dialog.open) return;
      this.protocols = protocols?.protocols || [];
      this.state.connections = list?.connections || [];
      this.state.canManage = Boolean(list?.can_manage);
      this.state.loading = false;
      if (add && this.state.canManage) this.state.view = "protocols";
      this.render();
      this.focusFirst();
    } catch (error) {
      if (this.state.workspaceId !== workspaceId) return;
      this.state.loading = false;
      this.state.loadError = error.message;
      this.render();
    }
  }

  close() {
    if (this.dialog.open) this.dialog.close();
  }

  protocol(id) {
    return (this.protocols || []).find((item) => item.id === id) || null;
  }

  notify() {
    this.onChanged?.(this.state.workspaceId, this.state.connections);
  }

  /* ---------------------------------------------------------------- render */

  render() {
    const { view } = this.state;
    if (view === "protocols") this.body.innerHTML = this.protocolsView();
    else if (view === "form") this.body.innerHTML = this.formView();
    else this.body.innerHTML = this.listView();
  }

  focusFirst() {
    requestAnimationFrame(() => {
      const target = this.body.querySelector("[data-cv-autofocus]") || this.body.querySelector("input:not([type=hidden]):not(:disabled), button:not(:disabled)");
      target?.focus({ preventScroll: true });
    });
  }

  header(title, description) {
    return `<div class="sheet-head cv-conn-head">
        <h2 id="cv-conn-title">${escapeHTML(title)}</h2>
        ${description ? `<p>${description}</p>` : ""}
      </div>`;
  }

  listView() {
    const state = this.state;
    const intro = `Use your own accounts for language, image and video models. ${escapeHTML(COPY)}`;
    let content;
    if (state.loading) {
      content = '<div class="cv-panel-loading"><span class="cv-spinner" aria-hidden="true"></span>Loading connections…</div>';
    } else if (state.loadError) {
      content = `<div class="cv-conn-error" role="alert">${icon("alert")}<span>${escapeHTML(state.loadError)}</span><button type="button" class="btn btn-secondary cv-btn-sm" data-cv-conn="reload">Try again</button></div>`;
    } else if (!state.connections.length) {
      content = `<div class="empty-block is-compact cv-conn-empty">
          <span class="empty-icon">${icon("plug", { size: 18 })}</span>
          <strong>No connections yet</strong>
          <p>${state.canManage
            ? "Add a key for OpenAI-compatible APIs, Anthropic, OpenRouter, Volcengine Ark or Alibaba Cloud, then pick it on any LLM, image or video node."
            : "Ask a workspace admin to add connections. You can use them on nodes once they exist."}</p>
          ${state.canManage ? '<div class="btn-row btn-row-center"><button type="button" class="btn btn-primary" data-cv-conn="add" data-cv-autofocus>Add connection</button></div>' : ""}
        </div>`;
    } else {
      content = `<ul class="cv-conn-list">${state.connections.map((connection) => this.connectionCard(connection)).join("")}</ul>`;
    }
    return `${this.header("API connections", intro)}
      ${state.notice ? `<p class="cv-conn-notice" role="status">${icon("check", { size: 14 })}<span>${escapeHTML(state.notice)}</span></p>` : ""}
      ${content}
      ${!state.loading && !state.loadError && !state.canManage && state.connections.length ? `<p class="cv-conn-readonly">${icon("shield", { size: 14 })}<span>Ask a workspace admin to add connections.</span></p>` : ""}
      <div class="sheet-actions">
        <button type="button" class="btn btn-tertiary" data-cv-conn="close">Close</button>
        ${state.canManage && state.connections.length ? `<button type="button" class="btn btn-primary" data-cv-conn="add">${icon("plus", { size: 14 })}Add connection</button>` : ""}
      </div>`;
  }

  connectionCard(connection) {
    const state = this.state;
    const test = state.tests.get(connection.id);
    const testing = state.testing.has(connection.id);
    const invalid = connection.status === "INVALID";
    const verified = connection.last_verified_at ? `Key checked ${relativeTime(connection.last_verified_at)}` : "Key not checked yet";
    const modelCount = (connection.models || []).length;
    const confirming = state.confirmDelete === connection.id;
    return `<li class="cv-conn-card${invalid ? " is-invalid" : ""}" data-cv-connection="${escapeHTML(connection.id)}">
        <div class="cv-conn-card-main">
          <div class="cv-conn-card-title">
            <strong>${escapeHTML(connection.name)}</strong>
            <span class="cv-conn-protocol">${escapeHTML(connection.protocol_label || connection.protocol)}</span>
          </div>
          <div class="cv-conn-card-facts">
            ${chips(connection.capabilities)}
            <span class="cv-conn-key mono" title="The key's first and last characters">${icon("key", { size: 13 })}${escapeHTML(connection.secret_hint || "••••")}</span>
            <span class="cv-status ${invalid ? "tone-danger" : connection.last_verified_at ? "tone-ok" : "tone-neutral"}">${invalid ? icon("alert", { size: 12 }) : connection.last_verified_at ? icon("check", { size: 12 }) : "<i></i>"}<span>${invalid ? "Key rejected" : escapeHTML(verified)}</span></span>
            <span class="cv-conn-models">${modelCount ? `${modelCount} model${modelCount === 1 ? "" : "s"}` : "No models listed"}</span>
          </div>
          ${invalid && connection.last_error && !test ? `<p class="cv-conn-last-error">${escapeHTML(connection.last_error)}</p>` : ""}
          ${test ? this.testResult(test) : ""}
        </div>
        <div class="cv-conn-card-actions">
          ${confirming ? `<div class="cv-conn-confirm" role="group" aria-label="Confirm delete">
              <span>Delete this connection? Nodes that use it will stop running.</span>
              <button type="button" class="btn btn-tertiary cv-btn-sm" data-cv-conn="cancel-delete">Cancel</button>
              <button type="button" class="btn btn-danger cv-btn-sm" data-cv-conn="confirm-delete"${state.busy ? " disabled" : ""}>Delete</button>
            </div>` : `
            <button type="button" class="btn btn-tertiary cv-btn-sm" data-cv-conn="test"${testing || !state.canManage ? " disabled" : ""} title="${state.canManage ? "Check the key with the provider" : "Testing needs editor access"}">${testing ? '<span class="cv-spinner" aria-hidden="true"></span>Testing…' : `${icon("refresh", { size: 13 })}Test`}</button>
            ${state.canManage ? `<button type="button" class="btn btn-tertiary cv-btn-sm" data-cv-conn="edit">${icon("pencil", { size: 13 })}Edit</button>
            <button type="button" class="btn btn-tertiary cv-btn-sm cv-danger-btn" data-cv-conn="delete" aria-label="Delete ${escapeHTML(connection.name)}">${icon("trash", { size: 13 })}</button>` : ""}`}
        </div>
      </li>`;
  }

  testResult(test) {
    const good = Boolean(test.ok);
    const latency = Number.isFinite(Number(test.latency_ms)) && test.latency_ms !== null ? ` · ${test.latency_ms} ms` : "";
    return `<p class="cv-conn-test ${good ? "tone-ok" : "tone-danger"}" role="status">${icon(good ? "check" : "alert", { size: 13 })}<span>${escapeHTML(test.message || (good ? "The connection works." : "The check failed."))}${escapeHTML(latency)}</span></p>`;
  }

  protocolsView() {
    const wanted = this.state.capability;
    const protocols = [...(this.protocols || [])].sort((a, b) => {
      if (!wanted) return 0;
      return Number(b.capabilities.includes(wanted)) - Number(a.capabilities.includes(wanted));
    });
    const wantedCopy = wanted ? ` Protocols that can run ${escapeHTML(capabilityLabel(wanted).toLowerCase())} models come first.` : "";
    return `${this.header("Add a connection", `Choose the kind of API your key belongs to.${wantedCopy}`)}
      <div class="cv-proto-grid">
        ${protocols.map((protocol, index) => {
          const fits = !wanted || protocol.capabilities.includes(wanted);
          return `<button type="button" class="cv-proto-card${fits ? "" : " is-dim"}" data-cv-protocol="${escapeHTML(protocol.id)}"${index === 0 ? " data-cv-autofocus" : ""}>
              <span class="cv-proto-title"><strong>${escapeHTML(protocol.label)}</strong>${protocol.development_only ? '<span class="cv-chip is-dev">Development</span>' : ""}</span>
              <span class="cv-proto-desc">${escapeHTML(protocol.description)}</span>
              ${chips(protocol.capabilities)}
            </button>`;
        }).join("")}
      </div>
      <div class="sheet-actions">
        <button type="button" class="btn btn-tertiary" data-cv-conn="back">Back</button>
      </div>`;
  }

  suggestions(form, protocol) {
    const preset = (protocol.presets || []).find((item) => item.id === form.presetId);
    const merged = {};
    for (const source of [preset?.suggested_models, protocol.suggested_models]) {
      for (const [capability, ids] of Object.entries(source || {})) {
        merged[capability] = [...new Set([...(merged[capability] || []), ...(ids || [])])];
      }
    }
    return merged;
  }

  formView() {
    const { form, error, busy } = this.state;
    const protocol = this.protocol(form.protocolId);
    if (!protocol) return this.listView();
    const editing = form.mode === "edit";
    const connection = editing ? this.state.connections.find((item) => item.id === form.connectionId) : null;
    const capabilities = protocol.capabilities || [];
    const chosen = capabilities.filter((capability) => form.capabilities.has(capability));
    const suggestions = this.suggestions(form, protocol);
    const suggestionGroups = Object.entries(suggestions)
      .filter(([capability, ids]) => form.capabilities.has(capability) && ids.some((id) => !form.models.some((model) => model.id === id && model.capability === capability)))
      .map(([capability, ids]) => `<div class="cv-suggest-group"><span class="cv-suggest-label">${escapeHTML(capabilityLabel(capability))}</span>${ids
        .filter((id) => !form.models.some((model) => model.id === id && model.capability === capability))
        .map((id) => `<button type="button" class="cv-chip is-suggest" data-cv-conn="suggest" data-cv-model="${escapeHTML(id)}" data-cv-capability="${escapeHTML(capability)}" title="Add ${escapeHTML(id)}">${icon("plus", { size: 11 })}${escapeHTML(id)}</button>`).join("")}</div>`)
      .join("");
    const multi = chosen.length > 1;
    const test = editing ? this.state.tests.get(form.connectionId) : null;
    const remote = this.state.remote;
    return `${this.header(editing ? `Edit “${connection?.name || form.name}”` : `New ${protocol.label} connection`, escapeHTML(protocol.description))}
      <form class="cv-conn-form" novalidate>
        ${this.state.notice ? `<p class="cv-conn-notice" role="status">${icon("check", { size: 14 })}<span>${escapeHTML(this.state.notice)}</span></p>` : ""}
        ${test ? this.testResult(test) : ""}
        <div class="cv-conn-grid">
          ${(protocol.presets || []).length ? `<label class="field"><span class="field-label">Provider</span>
            <select name="preset">
              <option value="">Custom</option>
              ${protocol.presets.map((preset) => `<option value="${escapeHTML(preset.id)}"${preset.id === form.presetId ? " selected" : ""}>${escapeHTML(preset.label)}</option>`).join("")}
            </select></label>` : ""}
          <label class="field"><span class="field-label">Name</span>
            <input name="name" maxlength="120" autocomplete="off" value="${escapeHTML(form.name)}" placeholder="e.g. Team OpenRouter" data-cv-autofocus /></label>
        </div>
        <label class="field"><span class="field-label">API key${editing ? "" : '<span class="cv-required" aria-hidden="true">*</span>'}</span>
          <span class="cv-secret">
            <input name="api_key" type="${form.showKey ? "text" : "password"}" autocomplete="off" autocapitalize="off" spellcheck="false"
              placeholder="${editing ? `Leave empty to keep ${escapeHTML(connection?.secret_hint || "the current key")}` : "Paste your key"}" />
            <button type="button" class="cv-icon-btn" data-cv-conn="toggle-key" aria-label="${form.showKey ? "Hide the key" : "Show the key"}" title="${form.showKey ? "Hide" : "Show"}">${icon(form.showKey ? "eye-off" : "eye", { size: 15 })}</button>
          </span>
          <span class="field-note">${editing ? "Type a new key only to replace the saved one." : "Stored encrypted. It is never shown again, to you or anyone else."}</span>
        </label>
        <label class="field"><span class="field-label">Base URL</span>
          <input name="base_url" type="text" inputmode="url" autocomplete="off" spellcheck="false" value="${escapeHTML(form.baseUrl)}" placeholder="${escapeHTML(protocol.default_base_url || "")}" />
          ${protocol.base_url_hint ? `<span class="field-note">${escapeHTML(protocol.base_url_hint)}</span>` : ""}
        </label>
        <fieldset class="cv-fieldset">
          <legend class="field-label">What this key can run</legend>
          <div class="cv-cap-row">
            ${capabilities.map((capability) => `<label class="check-row cv-check"><input type="checkbox" name="capability" value="${escapeHTML(capability)}"${form.capabilities.has(capability) ? " checked" : ""}${capabilities.length === 1 ? " disabled" : ""} /><span>${escapeHTML(capabilityLabel(capability))}</span></label>`).join("")}
          </div>
        </fieldset>
        <fieldset class="cv-fieldset">
          <legend class="field-label">Models</legend>
          <p class="field-note">The models your nodes can pick for this connection. A node can still use any model id your account has.</p>
          <ul class="cv-model-chips">${form.models.length ? form.models.map((model, index) => `
            <li class="cv-model-chip">
              <span class="mono">${escapeHTML(model.id)}</span>
              ${multi ? `<select aria-label="Capability of ${escapeHTML(model.id)}" data-cv-model-capability="${index}">${chosen.map((capability) => `<option value="${escapeHTML(capability)}"${capability === model.capability ? " selected" : ""}>${escapeHTML(capabilityLabel(capability))}</option>`).join("")}</select>` : ""}
              <button type="button" class="cv-icon-btn" data-cv-conn="remove-model" data-cv-index="${index}" aria-label="Remove ${escapeHTML(model.id)}">${icon("x", { size: 12 })}</button>
            </li>`).join("") : '<li class="cv-model-none">No models yet.</li>'}</ul>
          <div class="cv-model-add">
            <input name="model_draft" autocomplete="off" spellcheck="false" placeholder="Add a model id" value="${escapeHTML(form.modelDraft)}" aria-label="Model id to add" />
            ${multi ? `<select name="model_draft_capability" aria-label="Capability of the model to add">${chosen.map((capability) => `<option value="${escapeHTML(capability)}"${capability === form.draftCapability ? " selected" : ""}>${escapeHTML(capabilityLabel(capability))}</option>`).join("")}</select>` : ""}
            <button type="button" class="btn btn-secondary cv-btn-sm" data-cv-conn="add-model"${chosen.length ? "" : " disabled"}>Add</button>
          </div>
          ${suggestionGroups ? `<div class="cv-suggest"><span class="field-note">Suggested</span>${suggestionGroups}</div>` : ""}
          ${editing && protocol.lists_models ? `<div class="cv-remote">
              <button type="button" class="btn btn-tertiary cv-btn-sm" data-cv-conn="fetch-models"${remote?.loading ? " disabled" : ""}>${remote?.loading ? '<span class="cv-spinner" aria-hidden="true"></span>Fetching models…' : `${icon("refresh", { size: 13 })}Fetch models from provider`}</button>
              ${remote ? this.remotePicker(remote, chosen) : ""}
            </div>` : (!editing && protocol.lists_models ? '<p class="field-note">After saving you can fetch the model list from the provider.</p>' : "")}
        </fieldset>
        <p class="sheet-error" role="alert">${escapeHTML(error)}</p>
        <div class="sheet-actions">
          <button type="button" class="btn btn-tertiary" data-cv-conn="${editing ? "back-list" : "back"}"${busy ? " disabled" : ""}>${editing ? "Done" : "Back"}</button>
          <button type="submit" class="btn btn-primary"${busy ? " disabled" : ""}>${busy ? '<span class="cv-spinner" aria-hidden="true"></span>Saving…' : editing ? "Save changes" : "Save connection"}</button>
        </div>
      </form>`;
  }

  remotePicker(remote, chosen) {
    if (remote.loading) return "";
    if (remote.error) return `<p class="cv-conn-test tone-danger" role="alert">${icon("alert", { size: 13 })}<span>${escapeHTML(remote.error)}</span></p>`;
    const needle = remote.query.trim().toLowerCase();
    const form = this.state.form;
    const visible = remote.models.filter((model) => !needle || `${model.id} ${model.label || ""}`.toLowerCase().includes(needle)).slice(0, 300);
    const capability = remote.capability || chosen[0];
    return `<div class="cv-remote-picker">
        <div class="cv-remote-bar">
          <label class="cv-search">${icon("search", { size: 14 })}<input type="search" name="remote_query" value="${escapeHTML(remote.query)}" placeholder="Search ${remote.models.length} models" aria-label="Search the provider's models" /></label>
          ${chosen.length > 1 ? `<select name="remote_capability" aria-label="Add the chosen models as">${chosen.map((item) => `<option value="${escapeHTML(item)}"${item === capability ? " selected" : ""}>as ${escapeHTML(capabilityLabel(item))}</option>`).join("")}</select>` : ""}
        </div>
        <ul class="cv-remote-list" data-cv-scroll>${visible.length ? visible.map((model) => {
          const already = form.models.some((item) => item.id === model.id && item.capability === capability);
          return `<li><label class="check-row cv-check"><input type="checkbox" data-cv-remote-model="${escapeHTML(model.id)}"${remote.selected.has(model.id) || already ? " checked" : ""}${already ? " disabled" : ""} /><span class="mono">${escapeHTML(model.id)}</span>${model.label && model.label !== model.id ? `<small>${escapeHTML(model.label)}</small>` : ""}</label></li>`;
        }).join("") : '<li class="cv-model-none">No model matches.</li>'}</ul>
        <div class="btn-row">
          <button type="button" class="btn btn-secondary cv-btn-sm" data-cv-conn="add-remote"${remote.selected.size ? "" : " disabled"}>Add ${remote.selected.size || ""} model${remote.selected.size === 1 ? "" : "s"}</button>
          <button type="button" class="btn btn-tertiary cv-btn-sm" data-cv-conn="close-remote">Close list</button>
        </div>
      </div>`;
  }

  /* ---------------------------------------------------------------- form state */

  startCreate(protocolId) {
    const protocol = this.protocol(protocolId);
    if (!protocol) return;
    const capabilities = new Set(protocol.capabilities);
    this.state.form = {
      mode: "create",
      protocolId,
      presetId: "",
      name: protocol.label,
      nameTouched: false,
      showKey: false,
      baseUrl: protocol.default_base_url || "",
      baseTouched: false,
      capabilities,
      models: [],
      modelDraft: "",
      draftCapability: this.state.capability && capabilities.has(this.state.capability) ? this.state.capability : [...capabilities][0],
    };
    this.state.view = "form";
    this.state.error = "";
    this.state.notice = "";
    this.state.remote = null;
    this.render();
    this.focusFirst();
  }

  startEdit(connectionId, { notice = "" } = {}) {
    const connection = this.state.connections.find((item) => item.id === connectionId);
    const protocol = connection ? this.protocol(connection.protocol) : null;
    if (!connection || !protocol) return;
    const preset = (protocol.presets || []).find((item) => item.base_url === connection.base_url);
    this.state.form = {
      mode: "edit",
      connectionId,
      protocolId: connection.protocol,
      presetId: preset?.id || "",
      name: connection.name,
      nameTouched: true,
      showKey: false,
      baseUrl: connection.base_url || "",
      baseTouched: true,
      capabilities: new Set(connection.capabilities || []),
      models: (connection.models || []).map((model) => ({ id: model.id, capability: model.capability })),
      modelDraft: "",
      draftCapability: (connection.capabilities || [])[0],
    };
    this.state.view = "form";
    this.state.error = "";
    this.state.notice = notice;
    this.state.remote = null;
    this.render();
    this.focusFirst();
  }

  readTypedKey() {
    const input = this.body.querySelector('input[name="api_key"]');
    return input ? input.value : "";
  }

  /** Re-render the form without losing what was typed into the key field. */
  rerenderForm() {
    const key = this.readTypedKey();
    const active = document.activeElement;
    const activeName = active && this.body.contains(active) ? active.getAttribute("name") : null;
    this.render();
    const keyInput = this.body.querySelector('input[name="api_key"]');
    if (keyInput) keyInput.value = key;
    if (activeName) this.body.querySelector(`[name="${CSS.escape(activeName)}"]`)?.focus({ preventScroll: true });
  }

  addModel(id, capability) {
    const form = this.state.form;
    const value = String(id || "").replace(/\s+/g, " ").trim();
    if (!value) return false;
    const cap = form.capabilities.has(capability) ? capability : [...form.capabilities][0];
    if (!cap) {
      this.state.error = "Choose what the key can run before adding models.";
      return false;
    }
    if (value.length > 200) {
      this.state.error = "A model id is at most 200 characters.";
      return false;
    }
    if (!form.models.some((model) => model.id === value && model.capability === cap)) form.models.push({ id: value, capability: cap });
    return true;
  }

  /* ---------------------------------------------------------------- events */

  onKeyDown(event) {
    if (event.key === "Enter" && event.target.matches?.('input[name="model_draft"]')) {
      event.preventDefault();
      this.onClick({ target: this.body.querySelector('[data-cv-conn="add-model"]') });
    }
  }

  onInput(event) {
    const form = this.state.form;
    const target = event.target;
    if (target.name === "remote_query" && this.state.remote) {
      this.state.remote.query = target.value;
      const caret = target.selectionStart;
      this.rerenderForm();
      const search = this.body.querySelector('input[name="remote_query"]');
      if (search) {
        search.focus();
        search.setSelectionRange(caret, caret);
      }
      return;
    }
    if (!form) return;
    if (target.name === "name") {
      form.name = target.value;
      form.nameTouched = true;
    } else if (target.name === "base_url") {
      form.baseUrl = target.value;
      form.baseTouched = true;
    } else if (target.name === "model_draft") {
      form.modelDraft = target.value;
    }
    if (this.state.error) {
      this.state.error = "";
      const error = this.body.querySelector(".sheet-error");
      if (error) error.textContent = "";
    }
  }

  onChange(event) {
    const form = this.state.form;
    const target = event.target;
    if (target.matches("[data-cv-remote-model]") && this.state.remote) {
      if (target.checked) this.state.remote.selected.add(target.dataset.cvRemoteModel);
      else this.state.remote.selected.delete(target.dataset.cvRemoteModel);
      const add = this.body.querySelector('[data-cv-conn="add-remote"]');
      const count = this.state.remote.selected.size;
      if (add) {
        add.disabled = !count;
        add.textContent = `Add ${count || ""} model${count === 1 ? "" : "s"}`;
      }
      return;
    }
    if (target.name === "remote_capability" && this.state.remote) {
      this.state.remote.capability = target.value;
      this.rerenderForm();
      return;
    }
    if (!form) return;
    if (target.name === "preset") {
      const protocol = this.protocol(form.protocolId);
      const preset = (protocol?.presets || []).find((item) => item.id === target.value);
      form.presetId = target.value;
      if (preset) {
        form.baseUrl = preset.base_url;
        if (!form.nameTouched || form.mode === "create") {
          const previous = (protocol.presets || []).some((item) => item.label === form.name) || form.name === protocol.label;
          if (!form.nameTouched || previous) form.name = preset.label;
        }
      } else if (protocol) {
        form.baseUrl = form.baseUrl || protocol.default_base_url || "";
      }
      this.rerenderForm();
    } else if (target.name === "capability") {
      if (target.checked) form.capabilities.add(target.value);
      else form.capabilities.delete(target.value);
      form.models = form.models.filter((model) => form.capabilities.has(model.capability));
      if (!form.capabilities.has(form.draftCapability)) form.draftCapability = [...form.capabilities][0];
      this.rerenderForm();
    } else if (target.name === "model_draft_capability") {
      form.draftCapability = target.value;
    } else if (target.matches("[data-cv-model-capability]")) {
      const model = form.models[Number(target.dataset.cvModelCapability)];
      if (model) model.capability = target.value;
      this.rerenderForm();
    }
  }

  async onClick(event) {
    const button = event.target?.closest?.("[data-cv-conn], [data-cv-protocol]");
    if (!button || button.disabled) return;
    const state = this.state;
    if (button.dataset.cvProtocol) {
      this.startCreate(button.dataset.cvProtocol);
      return;
    }
    const action = button.dataset.cvConn;
    const card = button.closest("[data-cv-connection]");
    const connectionId = card?.dataset.cvConnection;
    switch (action) {
      case "close":
        this.close();
        break;
      case "reload":
        this.open({ workspaceId: state.workspaceId, capability: state.capability });
        break;
      case "add":
        state.view = "protocols";
        state.notice = "";
        this.render();
        this.focusFirst();
        break;
      case "back":
        state.form = null;
        state.error = "";
        state.view = state.view === "form" ? "protocols" : "list";
        this.render();
        this.focusFirst();
        break;
      case "back-list":
        state.form = null;
        state.error = "";
        state.remote = null;
        state.view = "list";
        this.render();
        this.focusFirst();
        break;
      case "edit":
        this.startEdit(connectionId);
        break;
      case "delete":
        state.confirmDelete = connectionId;
        this.render();
        this.body.querySelector('[data-cv-conn="cancel-delete"]')?.focus();
        break;
      case "cancel-delete":
        state.confirmDelete = null;
        this.render();
        break;
      case "confirm-delete":
        await this.remove(connectionId);
        break;
      case "test":
        await this.test(connectionId);
        break;
      case "toggle-key":
        if (state.form) {
          state.form.showKey = !state.form.showKey;
          this.rerenderForm();
          this.body.querySelector('input[name="api_key"]')?.focus();
        }
        break;
      case "add-model": {
        const form = state.form;
        if (!form) break;
        const draft = this.body.querySelector('input[name="model_draft"]')?.value ?? form.modelDraft;
        const capability = this.body.querySelector('select[name="model_draft_capability"]')?.value || form.draftCapability || [...form.capabilities][0];
        if (this.addModel(draft, capability)) form.modelDraft = "";
        this.rerenderForm();
        this.body.querySelector('input[name="model_draft"]')?.focus();
        break;
      }
      case "suggest":
        if (state.form && this.addModel(button.dataset.cvModel, button.dataset.cvCapability)) this.rerenderForm();
        break;
      case "remove-model":
        if (state.form) {
          state.form.models.splice(Number(button.dataset.cvIndex), 1);
          this.rerenderForm();
        }
        break;
      case "fetch-models":
        await this.fetchRemote();
        break;
      case "add-remote": {
        const remote = state.remote;
        const form = state.form;
        if (!remote || !form) break;
        const capability = remote.capability || [...form.capabilities][0];
        for (const id of remote.selected) this.addModel(id, capability);
        const added = remote.selected.size;
        remote.selected.clear();
        state.notice = `Added ${added} model${added === 1 ? "" : "s"}. Save changes to keep them.`;
        this.rerenderForm();
        break;
      }
      case "close-remote":
        state.remote = null;
        this.rerenderForm();
        break;
      default:
        break;
    }
  }

  async onSubmit(event) {
    event.preventDefault();
    const state = this.state;
    const form = state.form;
    if (!form || state.busy) return;
    const protocol = this.protocol(form.protocolId);
    const typedKey = this.readTypedKey().trim();
    const name = String(form.name || "").replace(/\s+/g, " ").trim();
    const draft = this.body.querySelector('input[name="model_draft"]')?.value.trim();
    if (draft) {
      const capability = this.body.querySelector('select[name="model_draft_capability"]')?.value || form.draftCapability;
      if (this.addModel(draft, capability)) form.modelDraft = "";
    }
    let problem = "";
    if (!name) problem = "Give the connection a name.";
    else if (form.mode === "create" && !typedKey) problem = "Paste the API key.";
    else if (!form.capabilities.size) problem = "Choose at least one thing this key can run.";
    if (problem) {
      state.error = problem;
      this.rerenderForm();
      const field = !name ? "name" : (!typedKey && form.mode === "create") ? "api_key" : null;
      if (field) this.body.querySelector(`[name="${field}"]`)?.focus();
      return;
    }
    const body = {
      name,
      base_url: form.baseUrl.trim() || protocol?.default_base_url || undefined,
      capabilities: [...form.capabilities],
      models: form.models.map((model) => ({ id: model.id, capability: model.capability })),
    };
    if (typedKey) body.api_key = typedKey;
    state.busy = true;
    state.error = "";
    this.rerenderForm();
    const workspaceId = state.workspaceId;
    try {
      if (form.mode === "create") {
        const created = await api.createConnection(workspaceId, { ...body, protocol: form.protocolId });
        if (this.state !== state) return;
        state.connections = [...state.connections, created];
        state.busy = false;
        this.notify();
        this.startEdit(created.id, { notice: "Connection saved. Checking the key…" });
        await this.test(created.id, { fromForm: true });
      } else {
        const updated = await api.updateConnection(workspaceId, form.connectionId, body);
        if (this.state !== state) return;
        state.connections = state.connections.map((item) => (item.id === updated.id ? updated : item));
        state.busy = false;
        this.notify();
        const keyChanged = Boolean(typedKey);
        state.form = null;
        state.remote = null;
        state.view = "list";
        state.notice = `Saved “${updated.name}”.`;
        this.render();
        if (keyChanged) await this.test(updated.id);
      }
    } catch (error) {
      if (this.state !== state) return;
      state.busy = false;
      state.error = error.status === 403
        ? "Only workspace owners and admins can manage connections."
        : error.message;
      this.rerenderForm();
    }
  }

  async test(connectionId, { fromForm = false } = {}) {
    const state = this.state;
    if (!connectionId || state.testing.has(connectionId)) return;
    state.testing.add(connectionId);
    if (!fromForm) this.render();
    try {
      const result = await api.testConnection(state.workspaceId, connectionId);
      if (this.state !== state) return;
      state.tests.set(connectionId, result);
      if (result?.connection) {
        state.connections = state.connections.map((item) => (item.id === connectionId ? result.connection : item));
        this.notify();
      }
      if (fromForm && state.form?.connectionId === connectionId) {
        state.notice = "Connection saved.";
      }
    } catch (error) {
      if (this.state !== state) return;
      state.tests.set(connectionId, { ok: false, message: error.message });
    } finally {
      if (this.state === state) {
        state.testing.delete(connectionId);
        if (state.view === "form") this.rerenderForm();
        else this.render();
      }
    }
  }

  async remove(connectionId) {
    const state = this.state;
    state.busy = true;
    this.render();
    try {
      await api.deleteConnection(state.workspaceId, connectionId);
      if (this.state !== state) return;
      const removed = state.connections.find((item) => item.id === connectionId);
      state.connections = state.connections.filter((item) => item.id !== connectionId);
      state.tests.delete(connectionId);
      state.notice = removed ? `Deleted “${removed.name}”.` : "Deleted.";
      this.notify();
    } catch (error) {
      if (this.state !== state) return;
      this.toast?.(error.status === 403 ? "Only workspace owners and admins can delete connections." : error.message);
    } finally {
      if (this.state === state) {
        state.busy = false;
        state.confirmDelete = null;
        this.render();
      }
    }
  }

  async fetchRemote() {
    const state = this.state;
    const form = state.form;
    if (!form || form.mode !== "edit") return;
    const chosen = [...form.capabilities];
    state.remote = { loading: true, error: "", models: [], query: "", selected: new Set(), capability: chosen[0] };
    this.rerenderForm();
    try {
      const result = await api.remoteModels(state.workspaceId, form.connectionId);
      if (this.state !== state || !state.remote) return;
      state.remote.models = (result?.models || []).filter((model) => model?.id);
      state.remote.loading = false;
      if (!state.remote.models.length) state.remote.error = "The provider listed no models for this key.";
    } catch (error) {
      if (this.state !== state || !state.remote) return;
      state.remote.loading = false;
      state.remote.error = error.message;
    }
    this.rerenderForm();
    this.body.querySelector('input[name="remote_query"]')?.focus();
  }
}
