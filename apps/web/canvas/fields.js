/**
 * Parameter fields, rendered from the catalogue's `Param` definitions.
 *
 * The same builder serves the node card (compact, `inline` params only) and
 * the inspector (every visible param). A field writes through `onChange`;
 * `sync` brings it up to date with the document without touching a control
 * the user is typing in, so the card and the inspector can edit the same
 * value side by side.
 */
import { icon } from "./icons.js";
import { escapeHTML, friendlyModel } from "./ui.js";

let fieldCounter = 0;
const nextId = (prefix) => `${prefix}-${(fieldCounter += 1)}`;

const PLAN_LOCK_COPY = "Pro plan";
const UNAVAILABLE_COPY = "unavailable right now";

/* Where CSS can size a textarea to its content it does (canvas.css), and no
   script runs. Elsewhere every pending textarea is measured in one frame -
   all writes, then all reads, then all writes - so opening a canvas of two
   hundred cards costs one layout, not two hundred. */
const FIELD_SIZING = typeof CSS !== "undefined" && typeof CSS.supports === "function"
  && CSS.supports("field-sizing", "content");
const pendingAutosize = new Map();
let autosizeFrame = 0;

function flushAutosize() {
  autosizeFrame = 0;
  const entries = [...pendingAutosize].filter(([textarea]) => textarea.isConnected);
  pendingAutosize.clear();
  for (const [textarea] of entries) textarea.style.height = "auto";
  const measured = entries.map(([textarea, max]) => [textarea, max, textarea.scrollHeight]);
  for (const [textarea, max, height] of measured) {
    textarea.style.height = `${Math.min(height + 2, max)}px`;
    textarea.style.overflowY = height + 2 > max ? "auto" : "hidden";
  }
}

function autosize(textarea, max, { now = false } = {}) {
  if (FIELD_SIZING || !textarea) return;
  pendingAutosize.set(textarea, max);
  if (now) {
    if (autosizeFrame) cancelAnimationFrame(autosizeFrame);
    flushAutosize();
    return;
  }
  if (!autosizeFrame) autosizeFrame = requestAnimationFrame(flushAutosize);
}

function isFocused(control) {
  return Boolean(control) && document.activeElement === control;
}

export function connectionOptions(connections, capability) {
  return (connections || []).filter((item) => !capability || (item.capabilities || []).includes(capability));
}

export function connectionModels(connection, capability) {
  return (connection?.models || []).filter((model) => !capability || model.capability === capability);
}

/**
 * Build one field. `ctx`:
 * - `value`, `data` (effective node data), `disabled`, `compact`
 * - `connected`: the input port of the same key has an edge (text params)
 * - `connectedFrom`: a label for where the text comes from
 * - `env`: `{connections, videoModels, canManageConnections, actions}`
 * - `onChange(key, value, {coalesce})`
 */
export function createField(param, ctx) {
  switch (param.kind) {
    case "textarea": return new TextField(param, ctx, true);
    case "text": return new TextField(param, ctx, false);
    case "number":
    case "integer": return new NumberField(param, ctx);
    case "select": return new SelectField(param, ctx);
    case "boolean": return new BooleanField(param, ctx);
    case "connection": return new ConnectionField(param, ctx);
    case "model": return new ModelField(param, ctx);
    case "platform_video_model": return new PlatformModelField(param, ctx);
    default: return new TextField(param, ctx, false);
  }
}

class BaseField {
  constructor(param, ctx) {
    this.param = param;
    this.ctx = ctx;
    this.id = nextId(ctx.compact ? "cv-card-field" : "cv-field");
    this.el = document.createElement("div");
    this.el.className = `cv-field${ctx.compact ? " is-compact" : ""}`;
    this.el.dataset.cvParam = param.key;
  }

  shell(control, { labelFirst = true, hideLabel = false } = {}) {
    const { param, ctx } = this;
    const label = hideLabel
      ? ""
      : `<label class="cv-field-label" for="${this.id}">${escapeHTML(param.label)}${param.required ? '<span class="cv-required" aria-hidden="true">*</span>' : ""}</label>`;
    const help = !ctx.compact && param.help ? `<p class="cv-field-help" id="${this.id}-help">${escapeHTML(param.help)}</p>` : "";
    this.el.innerHTML = `${labelFirst ? label : ""}${control}${labelFirst ? "" : label}${help}<p class="cv-field-error" id="${this.id}-error" role="alert"></p>`;
  }

  setInvalid(message) {
    const error = this.el.querySelector(".cv-field-error");
    this.el.classList.toggle("is-invalid", Boolean(message));
    if (error) error.textContent = message || "";
    const control = this.control;
    if (control) {
      if (message) control.setAttribute("aria-invalid", "true");
      else control.removeAttribute("aria-invalid");
      control.setAttribute("aria-describedby", `${this.id}-error${this.param.help && !this.ctx.compact ? ` ${this.id}-help` : ""}`);
    }
  }

  emit(value, options = {}) {
    this.ctx.onChange?.(this.param.key, value, options);
  }

  setDisabled(disabled) {
    if (this.control) this.control.disabled = Boolean(disabled);
  }
}

class TextField extends BaseField {
  constructor(param, ctx, multiline) {
    super(param, ctx);
    this.multiline = multiline;
    this.max = ctx.compact ? 220 : 320;
    this.render();
  }

  render() {
    const { param, ctx } = this;
    const hideLabel = ctx.compact && ctx.hideCompactLabel;
    if (ctx.connected && param.kind === "textarea") {
      this.shell(`<div class="cv-connected-hint" id="${this.id}">${icon("link", { size: 14 })}<span>Using connected text${ctx.connectedFrom ? ` from <b>${escapeHTML(ctx.connectedFrom)}</b>` : ""}</span></div>`, { hideLabel });
      this.control = null;
      this.connectedMode = true;
      return;
    }
    this.connectedMode = false;
    const common = `id="${this.id}" name="${escapeHTML(param.key)}" ${param.max_length ? `maxlength="${param.max_length}"` : ""} placeholder="${escapeHTML(param.placeholder || "")}" spellcheck="${param.key === "model" ? "false" : "true"}"`;
    this.shell(this.multiline
      ? `<textarea ${common} rows="${ctx.compact ? 3 : 4}" data-cv-nodrag></textarea>`
      : `<input type="text" ${common} autocomplete="off" data-cv-nodrag />`, { hideLabel });
    this.control = this.el.querySelector("textarea, input");
    this.control.value = typeof ctx.value === "string" ? ctx.value : (ctx.value ?? "");
    this.control.disabled = Boolean(ctx.disabled);
    if (hideLabel) this.control.setAttribute("aria-label", param.label);
    this.control.addEventListener("input", () => {
      if (this.multiline) autosize(this.control, this.max, { now: true });
      this.emit(this.control.value, { coalesce: true });
    });
    if (this.multiline) autosize(this.control, this.max);
  }

  sync(value, ctx) {
    const connectedChanged = Boolean(ctx.connected && this.param.kind === "textarea") !== this.connectedMode;
    this.ctx = { ...this.ctx, ...ctx, value };
    if (connectedChanged) {
      this.render();
      return;
    }
    if (!this.control) {
      const from = this.el.querySelector(".cv-connected-hint b");
      if (from && ctx.connectedFrom) from.textContent = ctx.connectedFrom;
      return;
    }
    this.control.disabled = Boolean(ctx.disabled);
    const text = typeof value === "string" ? value : (value ?? "");
    if (!isFocused(this.control) && this.control.value !== text) {
      this.control.value = text;
      if (this.multiline) autosize(this.control, this.max);
    }
  }

  refit() {
    if (this.multiline && this.control) autosize(this.control, this.max);
  }
}

class NumberField extends BaseField {
  constructor(param, ctx) {
    super(param, ctx);
    const step = param.step ?? (param.kind === "integer" ? 1 : "any");
    this.shell(`<input type="number" id="${this.id}" name="${escapeHTML(param.key)}" inputmode="${param.kind === "integer" ? "numeric" : "decimal"}"
      ${param.min !== null && param.min !== undefined ? `min="${param.min}"` : ""}
      ${param.max !== null && param.max !== undefined ? `max="${param.max}"` : ""}
      step="${step}" placeholder="${escapeHTML(param.placeholder || (param.default === null || param.default === undefined ? "Provider default" : String(param.default)))}" data-cv-nodrag />`);
    this.control = this.el.querySelector("input");
    this.control.value = ctx.value === null || ctx.value === undefined ? "" : String(ctx.value);
    this.control.disabled = Boolean(ctx.disabled);
    this.control.addEventListener("input", () => this.commit(true));
    this.control.addEventListener("change", () => this.commit(false));
  }

  parse() {
    const raw = this.control.value.trim();
    if (raw === "") return { ok: true, value: null };
    const number = Number(raw);
    if (!Number.isFinite(number)) return { ok: false, message: `${this.param.label} must be a number` };
    if (this.param.kind === "integer" && !Number.isInteger(number)) {
      return { ok: false, message: `${this.param.label} must be a whole number` };
    }
    if (this.param.min !== null && this.param.min !== undefined && number < this.param.min) {
      return { ok: false, message: `${this.param.label} must be at least ${this.param.min}` };
    }
    if (this.param.max !== null && this.param.max !== undefined && number > this.param.max) {
      return { ok: false, message: `${this.param.label} must be at most ${this.param.max}` };
    }
    return { ok: true, value: number };
  }

  commit(coalesce) {
    const parsed = this.parse();
    this.localError = parsed.ok ? null : parsed.message;
    this.setInvalid(this.localError || this.ctx.invalid || null);
    if (parsed.ok) this.emit(parsed.value, { coalesce });
  }

  sync(value, ctx) {
    this.ctx = { ...this.ctx, ...ctx, value };
    this.control.disabled = Boolean(ctx.disabled);
    if (isFocused(this.control)) return;
    const text = value === null || value === undefined ? "" : String(value);
    if (this.control.value !== text && !this.localError) this.control.value = text;
  }
}

class SelectField extends BaseField {
  constructor(param, ctx) {
    super(param, ctx);
    this.shell(`<select id="${this.id}" name="${escapeHTML(param.key)}" data-cv-nodrag></select>`);
    this.control = this.el.querySelector("select");
    this.renderOptions(ctx.value);
    this.control.disabled = Boolean(ctx.disabled);
    this.control.addEventListener("change", () => this.emit(this.control.value));
  }

  renderOptions(value) {
    const options = this.param.options || [];
    const current = value ?? this.param.default ?? "";
    const known = options.some((option) => option.value === current);
    this.control.innerHTML = (known || current === "" ? "" : `<option value="${escapeHTML(current)}">${escapeHTML(current)}</option>`)
      + options.map((option) => `<option value="${escapeHTML(option.value)}">${escapeHTML(option.label)}</option>`).join("");
    this.control.value = current;
  }

  sync(value, ctx) {
    this.ctx = { ...this.ctx, ...ctx, value };
    this.control.disabled = Boolean(ctx.disabled);
    if (isFocused(this.control)) return;
    if (this.control.value !== (value ?? this.param.default ?? "")) this.renderOptions(value);
  }
}

class BooleanField extends BaseField {
  constructor(param, ctx) {
    super(param, ctx);
    const help = !ctx.compact && param.help ? `<p class="cv-field-help" id="${this.id}-help">${escapeHTML(param.help)}</p>` : "";
    this.el.innerHTML = `<label class="check-row cv-check" for="${this.id}"><input type="checkbox" id="${this.id}" name="${escapeHTML(param.key)}" data-cv-nodrag /><span>${escapeHTML(param.label)}</span></label>${help}<p class="cv-field-error" id="${this.id}-error" role="alert"></p>`;
    this.control = this.el.querySelector("input");
    this.control.checked = Boolean(ctx.value);
    this.control.disabled = Boolean(ctx.disabled);
    this.control.addEventListener("change", () => this.emit(this.control.checked));
  }

  sync(value, ctx) {
    this.ctx = { ...this.ctx, ...ctx, value };
    this.control.disabled = Boolean(ctx.disabled);
    this.control.checked = Boolean(value);
  }
}

const ADD_CONNECTION = "__cv_add_connection__";

class ConnectionField extends BaseField {
  constructor(param, ctx) {
    super(param, ctx);
    this.shell(`<select id="${this.id}" name="${escapeHTML(param.key)}" data-cv-nodrag></select><p class="cv-field-note" hidden></p>`);
    this.control = this.el.querySelector("select");
    this.note = this.el.querySelector(".cv-field-note");
    this.renderOptions();
    this.control.addEventListener("change", () => {
      if (this.control.value === ADD_CONNECTION) {
        this.control.value = this.ctx.value || "";
        this.ctx.env?.actions?.openConnections?.({ capability: this.param.capability });
        return;
      }
      this.emit(this.control.value || null);
    });
  }

  renderOptions() {
    const { ctx, param } = this;
    const env = ctx.env || {};
    const all = env.connections || [];
    const options = connectionOptions(all, param.capability);
    const value = ctx.value || "";
    const current = all.find((item) => item.id === value);
    const capabilityCopy = { chat: "language model", image: "image", video: "video" }[param.capability] || param.capability;
    let markup = `<option value="">${options.length ? "Choose a connection…" : "No connections yet"}</option>`;
    if (value && !options.some((item) => item.id === value)) {
      markup += `<option value="${escapeHTML(value)}">${current ? `${escapeHTML(current.name)} (cannot run ${escapeHTML(capabilityCopy)})` : "Missing connection"}</option>`;
    }
    markup += options.map((item) => `<option value="${escapeHTML(item.id)}">${escapeHTML(item.name)} · ${escapeHTML(item.protocol_label || item.protocol)}${item.status === "INVALID" ? " (key rejected)" : ""}</option>`).join("");
    if (env.actions?.openConnections) markup += `<option value="${ADD_CONNECTION}">${env.canManageConnections === false ? "View API connections…" : "Add a connection…"}</option>`;
    this.control.innerHTML = markup;
    this.control.value = value;
    this.control.disabled = Boolean(ctx.disabled);
    const noteText = !options.length
      ? `Add an API connection that can run ${capabilityCopy} models.`
      : (current?.status === "INVALID" ? "The provider rejected this key the last time it was checked." : "");
    this.note.hidden = ctx.compact || !noteText;
    this.note.textContent = noteText;
  }

  sync(value, ctx) {
    this.ctx = { ...this.ctx, ...ctx, value };
    if (isFocused(this.control)) return;
    this.renderOptions();
  }
}

class ModelField extends BaseField {
  constructor(param, ctx) {
    super(param, ctx);
    this.listId = `${this.id}-models`;
    this.shell(`<div class="cv-combo"><input type="text" id="${this.id}" name="${escapeHTML(param.key)}" list="${this.listId}" autocomplete="off" spellcheck="false" data-cv-nodrag /><datalist id="${this.listId}"></datalist></div>`);
    this.control = this.el.querySelector("input");
    this.datalist = this.el.querySelector("datalist");
    this.control.value = ctx.value || "";
    this.control.addEventListener("input", () => this.emit(this.control.value, { coalesce: true }));
    this.control.addEventListener("change", () => {
      const trimmed = this.control.value.trim();
      if (trimmed !== this.control.value) {
        this.control.value = trimmed;
        this.emit(trimmed);
      }
    });
    this.renderOptions();
  }

  renderOptions() {
    const { ctx, param } = this;
    const connection = (ctx.env?.connections || []).find((item) => item.id === ctx.data?.connection_id);
    const models = connectionModels(connection, param.capability);
    this.datalist.innerHTML = models.map((model) => `<option value="${escapeHTML(model.id)}"></option>`).join("");
    const needsConnection = !ctx.data?.connection_id;
    this.control.disabled = Boolean(ctx.disabled) || (needsConnection && !ctx.value);
    this.control.placeholder = needsConnection
      ? "Choose a connection first"
      : (models.length ? "Pick or type a model id" : "Type a model id");
  }

  sync(value, ctx) {
    this.ctx = { ...this.ctx, ...ctx, value };
    this.renderOptions();
    if (!isFocused(this.control) && this.control.value !== (value || "")) this.control.value = value || "";
  }
}

class PlatformModelField extends BaseField {
  constructor(param, ctx) {
    super(param, ctx);
    this.shell(`<select id="${this.id}" name="${escapeHTML(param.key)}" data-cv-nodrag></select>`);
    this.control = this.el.querySelector("select");
    this.renderOptions();
    this.control.addEventListener("change", () => this.emit(this.control.value));
  }

  renderOptions() {
    const { ctx } = this;
    const models = ctx.env?.videoModels || [];
    const value = ctx.value || "";
    const values = models.map((model) => `${model.provider}:${model.model_id}`);
    let markup = '<option value="">Automatic (plan default)</option>';
    if (value && !values.includes(value)) {
      const modelId = value.slice(value.indexOf(":") + 1);
      markup += `<option value="${escapeHTML(value)}">${escapeHTML(friendlyModel(modelId))} (not offered now)</option>`;
    }
    markup += models.map((model) => {
      const locked = Boolean(model.plan_locked);
      const unavailable = model.available === false;
      const suffix = locked ? ` · ${PLAN_LOCK_COPY}` : (unavailable ? ` · ${UNAVAILABLE_COPY}` : "");
      const optionValue = `${model.provider}:${model.model_id}`;
      const disabled = (locked || unavailable) && optionValue !== value;
      return `<option value="${escapeHTML(optionValue)}"${disabled ? " disabled" : ""}>${escapeHTML(friendlyModel(model.model_id))}${escapeHTML(suffix)}</option>`;
    }).join("");
    this.control.innerHTML = markup;
    this.control.value = value;
    this.control.disabled = Boolean(ctx.disabled);
  }

  sync(value, ctx) {
    this.ctx = { ...this.ctx, ...ctx, value };
    if (isFocused(this.control)) return;
    this.renderOptions();
  }
}
