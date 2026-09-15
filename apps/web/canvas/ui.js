/**
 * Small UI primitives shared by the canvas modules: escaping, a toast, a
 * keyboard-navigable popup menu, confirm/prompt sheets, time formatting and
 * the public names of platform models.
 */
import { icon } from "./icons.js";

export const escapeHTML = (value = "") => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[character]);

export const isMac = typeof navigator !== "undefined"
  && /Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent || "");
export const MOD = isMac ? "⌘" : "Ctrl";
export const SHIFT = isMac ? "⇧" : "Shift";

export const prefersReducedMotion = () => typeof matchMedia === "function"
  && matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Create an element from an HTML string (one root). */
export function html(markup) {
  const template = document.createElement("template");
  template.innerHTML = String(markup).trim();
  return template.content.firstElementChild;
}

export function isEditableTarget(target) {
  if (!target || !(target instanceof Element)) return false;
  if (target.isContentEditable) return true;
  if (target.matches("textarea, select")) return true;
  if (target.matches("input")) {
    const type = (target.getAttribute("type") || "text").toLowerCase();
    return !["button", "checkbox", "radio", "range", "submit", "reset", "file", "color"].includes(type);
  }
  return false;
}

/* ------------------------------------------------------------------ time */

export function relativeTime(iso, now = Date.now()) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const seconds = Math.round((now - then) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days} d ago`;
  return new Date(then).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function clockTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (!Number.isFinite(date.getTime())) return "";
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/* ------------------------------------------------------------------ platform names */

/* The public names of the platform's models, the same table the workbench
   shows (app.js MODEL_LABELS): raw provider model ids are backend facts, and
   a route with no row reads "BestShiny model". Keep the two in step. */
const MODEL_LABELS = {
  "doubao-seedream-5-0-260128": "Shiny",
  NARWHAL: "Shinier",
  "openai/gpt-image-2": "Shiniest",
  "doubao-seedance-2-5-260628": "Shiny Motion · Cinematic",
  "x-ai/grok-imagine-video": "Shiny Motion · Stylised",
  "google/veo-3.1-lite": "Shiny Motion · Draft",
  "google/veo-3.1-fast": "Shinier Motion · Fast",
  "kwaivgi/kling-v3.0-std": "Shinier Motion · Continuity",
  "alibaba/wan-3.0": "Shinier Motion · Long take",
  "wan2.7-t2v-2026-06-12": "Shinier Motion · Long take",
  "wan-2.7": "Shinier Motion · Long take",
  "google/veo-3.1": "Shiniest Motion · Cinematic",
  "kwaivgi/kling-v3.0-pro": "Shiniest Motion · Continuity",
  "flow-veo-3.1": "Shiniest Motion · Studio",
};

export const friendlyModel = (modelId) => MODEL_LABELS[modelId] || (modelId ? "BestShiny model" : "Automatic");

/** `"provider:model_id"` -> `{provider, modelId}`; "" is automatic. */
export function parsePlatformModel(value) {
  const text = typeof value === "string" ? value : "";
  const at = text.indexOf(":");
  if (at <= 0) return null;
  return { provider: text.slice(0, at), modelId: text.slice(at + 1) };
}

/* ------------------------------------------------------------------ toast */

let toastNode = null;
let toastTimer = null;

export function toast(message, { action = null, tone = "neutral", duration = 3600 } = {}) {
  if (!message || typeof document === "undefined") return;
  if (!toastNode) {
    toastNode = document.createElement("div");
    toastNode.className = "cv-toast";
    toastNode.setAttribute("role", "status");
    toastNode.setAttribute("aria-live", "polite");
    if ("popover" in HTMLElement.prototype) toastNode.setAttribute("popover", "manual");
    document.body.append(toastNode);
  }
  toastNode.dataset.tone = tone;
  toastNode.innerHTML = `<span class="cv-toast-text">${escapeHTML(message)}</span>`;
  if (action?.label) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cv-toast-action";
    button.textContent = action.label;
    button.addEventListener("click", () => {
      hideToast();
      action.onClick?.();
    });
    toastNode.append(button);
  }
  const close = document.createElement("button");
  close.type = "button";
  close.className = "cv-toast-close";
  close.setAttribute("aria-label", "Dismiss");
  close.innerHTML = icon("x", { size: 14 });
  close.addEventListener("click", hideToast);
  toastNode.append(close);
  // A modal <dialog> sits in the top layer; a popover is the only way over it.
  if (typeof toastNode.showPopover === "function") {
    try {
      if (toastNode.matches(":popover-open")) toastNode.hidePopover();
      toastNode.showPopover();
    } catch (_error) { /* a race with another toast */ }
  }
  toastNode.classList.add("is-shown");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(hideToast, action ? Math.max(duration, 6000) : duration);
}

export function hideToast() {
  if (!toastNode) return;
  window.clearTimeout(toastTimer);
  toastNode.classList.remove("is-shown");
  if (typeof toastNode.hidePopover === "function") {
    try { if (toastNode.matches(":popover-open")) toastNode.hidePopover(); } catch (_error) { /* gone */ }
  }
}

/* ------------------------------------------------------------------ popup menu */

/**
 * A menu positioned in a container's coordinates.
 *
 * `items`: `[{label, description?, icon?, shortcut?, danger?, disabled?, group?, onSelect}]`
 * or `{separator: true}`. With `search`, a filter box sits on top and typing
 * narrows the items by label and description.
 */
export class PopupMenu {
  constructor(container) {
    this.container = container;
    this.el = document.createElement("div");
    this.el.className = "cv-menu";
    this.el.hidden = true;
    this.el.setAttribute("role", "menu");
    container.append(this.el);
    this.items = [];
    this.active = -1;
    this.onClose = null;
    this.returnFocus = null;
    this.el.addEventListener("keydown", (event) => this.onKeyDown(event));
    this.el.addEventListener("pointerdown", (event) => event.stopPropagation());
    this.el.addEventListener("contextmenu", (event) => event.preventDefault());
    this.el.addEventListener("wheel", (event) => event.stopPropagation(), { passive: true });
    this.outside = (event) => {
      if (!this.el.hidden && !this.el.contains(event.target) && !this.anchor?.contains?.(event.target)) this.close();
    };
    document.addEventListener("pointerdown", this.outside, true);
  }

  get open() {
    return !this.el.hidden;
  }

  contains(node) {
    return this.el.contains(node);
  }

  /** `x`, `y` in container coordinates; `anchor` keeps its own clicks from closing the menu. */
  show({ x, y, items, title = "", search = null, onClose = null, anchor = null, minWidth = 220, className = "" }) {
    this.close({ silent: true });
    this.returnFocus = document.activeElement;
    this.allItems = items;
    this.onClose = onClose;
    this.anchor = anchor;
    this.el.className = `cv-menu${className ? ` ${className}` : ""}`;
    this.el.style.minWidth = `${minWidth}px`;
    this.el.innerHTML = `
      ${title ? `<div class="cv-menu-title">${escapeHTML(title)}</div>` : ""}
      ${search ? `<div class="cv-menu-search">${icon("search", { size: 14 })}<input type="search" placeholder="${escapeHTML(search.placeholder || "Search")}" aria-label="${escapeHTML(search.placeholder || "Search")}" /></div>` : ""}
      <div class="cv-menu-list" role="none"></div>`;
    this.list = this.el.querySelector(".cv-menu-list");
    this.searchInput = this.el.querySelector(".cv-menu-search input");
    this.searchInput?.addEventListener("input", () => this.renderItems(this.searchInput.value));
    this.renderItems("");
    this.el.hidden = false;
    this.place(x, y);
    requestAnimationFrame(() => {
      if (this.searchInput) this.searchInput.focus({ preventScroll: true });
      else this.focusItem(this.firstEnabled());
    });
  }

  place(x, y) {
    const bounds = this.container.getBoundingClientRect();
    const width = this.el.offsetWidth;
    const height = this.el.offsetHeight;
    const left = Math.max(8, Math.min(x, bounds.width - width - 8));
    const top = Math.max(8, Math.min(y, bounds.height - height - 8));
    this.el.style.left = `${left}px`;
    this.el.style.top = `${top}px`;
  }

  renderItems(query) {
    const needle = String(query || "").trim().toLowerCase();
    const matches = (item) => item.separator
      || !needle
      || `${item.label} ${item.description || ""} ${item.keywords || ""}`.toLowerCase().includes(needle);
    const visible = this.allItems.filter(matches);
    // Drop separators and group headings that no longer head anything.
    const cleaned = [];
    for (const item of visible) {
      if (item.separator && (!cleaned.length || cleaned[cleaned.length - 1].separator)) continue;
      cleaned.push(item);
    }
    while (cleaned.length && cleaned[cleaned.length - 1].separator) cleaned.pop();
    this.items = cleaned;
    let lastGroup = null;
    this.list.innerHTML = cleaned.length ? cleaned.map((item, index) => {
      if (item.separator) return '<div class="cv-menu-sep" role="separator"></div>';
      const heading = item.group && item.group !== lastGroup ? `<div class="cv-menu-group">${escapeHTML(item.group)}</div>` : "";
      lastGroup = item.group || lastGroup;
      return `${heading}<button type="button" class="cv-menu-item${item.danger ? " is-danger" : ""}" role="menuitem" data-cv-menu-index="${index}" ${item.disabled ? 'disabled aria-disabled="true"' : ""} tabindex="-1">
        ${item.icon ? `<span class="cv-menu-icon">${icon(item.icon, { size: 15 })}</span>` : ""}
        <span class="cv-menu-text"><span class="cv-menu-label">${escapeHTML(item.label)}</span>${item.description ? `<span class="cv-menu-desc">${escapeHTML(item.description)}</span>` : ""}</span>
        ${item.shortcut ? `<kbd class="cv-menu-kbd">${escapeHTML(item.shortcut)}</kbd>` : ""}
      </button>`;
    }).join("") : `<div class="cv-menu-empty">${needle ? "Nothing matches" : "Nothing to add here"}</div>`;
    this.list.querySelectorAll("[data-cv-menu-index]").forEach((button) => {
      button.addEventListener("click", () => this.choose(Number(button.dataset.cvMenuIndex)));
      button.addEventListener("pointermove", () => {
        if (!button.disabled) this.setActive(Number(button.dataset.cvMenuIndex), { focus: !this.searchInput });
      });
    });
    this.setActive(this.firstEnabled(), { focus: false });
  }

  firstEnabled() {
    return this.items.findIndex((item) => !item.separator && !item.disabled);
  }

  setActive(index, { focus = true } = {}) {
    this.active = index;
    this.list.querySelectorAll("[data-cv-menu-index]").forEach((button) => {
      button.classList.toggle("is-active", Number(button.dataset.cvMenuIndex) === index);
    });
    if (focus) this.focusItem(index);
  }

  focusItem(index) {
    const button = this.list?.querySelector(`[data-cv-menu-index="${index}"]`);
    if (button) {
      button.focus({ preventScroll: true });
      button.scrollIntoView({ block: "nearest" });
    }
  }

  move(step) {
    if (!this.items.length) return;
    let index = this.active;
    for (let tries = 0; tries < this.items.length; tries += 1) {
      index = (index + step + this.items.length) % this.items.length;
      const item = this.items[index];
      if (!item.separator && !item.disabled) break;
    }
    this.setActive(index, { focus: !this.searchInput });
    this.list.querySelector(`[data-cv-menu-index="${index}"]`)?.scrollIntoView({ block: "nearest" });
  }

  onKeyDown(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.close();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      this.move(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      this.move(-1);
    } else if (event.key === "Enter") {
      if (this.active >= 0) {
        event.preventDefault();
        this.choose(this.active);
      }
    } else if (event.key === "Tab") {
      event.preventDefault();
      this.move(event.shiftKey ? -1 : 1);
    }
    event.stopPropagation();
  }

  choose(index) {
    const item = this.items[index];
    if (!item || item.separator || item.disabled) return;
    this.close({ restoreFocus: false });
    item.onSelect?.();
  }

  close({ silent = false, restoreFocus = true } = {}) {
    if (this.el.hidden) return;
    this.el.hidden = true;
    this.el.innerHTML = "";
    const onClose = this.onClose;
    this.onClose = null;
    this.anchor = null;
    if (restoreFocus && this.returnFocus?.isConnected && this.el.contains(document.activeElement) === false) {
      try { this.returnFocus.focus({ preventScroll: true }); } catch (_error) { /* gone */ }
    }
    if (!silent) onClose?.();
  }

  destroy() {
    document.removeEventListener("pointerdown", this.outside, true);
    this.el.remove();
  }
}

/* ------------------------------------------------------------------ sheets */

function sheet(markup) {
  const dialog = document.createElement("dialog");
  dialog.className = "sheet cv-sheet";
  dialog.innerHTML = markup;
  document.body.append(dialog);
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close("cancel");
  });
  return dialog;
}

/** A confirm sheet. Resolves true only on the confirm button. */
export function confirmSheet({ title, message, confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    const dialog = sheet(`
      <form method="dialog" class="sheet-body">
        <div class="sheet-head"><h2>${escapeHTML(title)}</h2>${message ? `<p>${escapeHTML(message)}</p>` : ""}</div>
        <div class="sheet-actions">
          <button class="btn btn-tertiary" value="cancel" type="submit">Cancel</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"}" value="confirm" type="submit">${escapeHTML(confirmLabel)}</button>
        </div>
      </form>`);
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "confirm");
      dialog.remove();
    });
    dialog.showModal();
    dialog.querySelector(`button[value="${danger ? "cancel" : "confirm"}"]`)?.focus();
  });
}

/**
 * A one-field sheet. `onSubmit(value)` may throw to keep the sheet open with
 * the message; resolves with whatever `onSubmit` returned, or null on cancel.
 */
export function promptSheet({ title, message = "", label, value = "", confirmLabel = "Save", maxLength = 200, placeholder = "", onSubmit }) {
  return new Promise((resolve) => {
    const dialog = sheet(`
      <form class="sheet-body" novalidate>
        <div class="sheet-head"><h2>${escapeHTML(title)}</h2>${message ? `<p>${escapeHTML(message)}</p>` : ""}</div>
        <label class="field"><span class="field-label">${escapeHTML(label)}</span>
          <input name="value" maxlength="${maxLength}" autocomplete="off" placeholder="${escapeHTML(placeholder)}" />
        </label>
        <p class="sheet-error" role="alert"></p>
        <div class="sheet-actions">
          <button class="btn btn-tertiary" type="button" data-cv-cancel>Cancel</button>
          <button class="btn btn-primary" type="submit">${escapeHTML(confirmLabel)}</button>
        </div>
      </form>`);
    const form = dialog.querySelector("form");
    const input = form.elements.value;
    const error = form.querySelector(".sheet-error");
    const submit = form.querySelector('button[type="submit"]');
    let result = null;
    let busy = false;
    input.value = value;
    form.querySelector("[data-cv-cancel]").addEventListener("click", () => dialog.close());
    dialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy) return;
      const text = input.value.trim();
      if (!text) {
        error.textContent = `${label} cannot be empty`;
        input.setAttribute("aria-invalid", "true");
        input.focus();
        return;
      }
      busy = true;
      submit.disabled = true;
      error.textContent = "";
      try {
        result = onSubmit ? await onSubmit(text) : text;
        busy = false;
        dialog.close();
      } catch (failure) {
        error.textContent = failure?.message || "That did not work";
      } finally {
        busy = false;
        submit.disabled = false;
      }
    });
    dialog.addEventListener("close", () => {
      resolve(result);
      dialog.remove();
    });
    dialog.showModal();
    requestAnimationFrame(() => { input.focus(); input.select(); });
  });
}

/** Copy text; true when it reached the clipboard. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(String(text ?? ""));
    return true;
  } catch (_error) {
    const area = document.createElement("textarea");
    area.value = String(text ?? "");
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_failure) { ok = false; }
    area.remove();
    return ok;
  }
}
