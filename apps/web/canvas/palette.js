/**
 * The node palette: every catalogue node type, grouped by category and
 * searchable. Drag one onto the canvas (a pointer drag with a ghost card, so
 * it works the same with a mouse, a pen or a finger), or click it - or press
 * Enter on it - to add it at the centre of the view.
 */
import { icon, hasIcon } from "./icons.js";
import { escapeHTML } from "./ui.js";

const DRAG_THRESHOLD = 4;

export class Palette {
  constructor(host, { onAdd, onDropAt, isInsideCanvas, onCollapse }) {
    this.host = host;
    this.onAdd = onAdd;
    this.onDropAt = onDropAt;
    this.isInsideCanvas = isInsideCanvas;
    this.onCollapse = onCollapse;
    this.catalog = null;
    this.readOnly = false;
    this.query = "";
    this.drag = null;
    host.innerHTML = `
      <div class="cv-panel-head">
        <h2 class="cv-panel-title">Nodes</h2>
        <button type="button" class="cv-icon-btn" data-cv-palette-collapse aria-label="Hide the nodes panel" title="Hide panel">${icon("chevron-left", { size: 15 })}</button>
      </div>
      <label class="cv-search">${icon("search", { size: 14 })}<input type="search" placeholder="Search nodes" aria-label="Search nodes" autocomplete="off" /></label>
      <div class="cv-palette-list"></div>
      <p class="cv-palette-tip">${icon("info", { size: 13 })}<span>Drag a node onto the canvas, or right-click the canvas to add one where you click.</span></p>`;
    this.list = host.querySelector(".cv-palette-list");
    this.search = host.querySelector('input[type="search"]');
    this.search.addEventListener("input", () => {
      this.query = this.search.value;
      this.render();
    });
    this.search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        const first = this.list.querySelector("[data-cv-type]:not(:disabled)");
        if (first) {
          event.preventDefault();
          this.onAdd?.(first.dataset.cvType);
        }
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        this.list.querySelector("[data-cv-type]")?.focus();
      }
    });
    host.querySelector("[data-cv-palette-collapse]").addEventListener("click", () => this.onCollapse?.());
    this.list.addEventListener("keydown", (event) => this.onListKey(event));
    this.list.addEventListener("click", (event) => {
      const item = event.target.closest("[data-cv-type]");
      if (!item || item.disabled) return;
      if (this.suppressClick) {
        this.suppressClick = false;
        return;
      }
      this.onAdd?.(item.dataset.cvType);
    });
    this.list.addEventListener("pointerdown", (event) => this.onPointerDown(event));
    this.move = (event) => this.onPointerMove(event);
    this.up = (event) => this.onPointerUp(event);
  }

  setCatalog(catalog) {
    this.catalog = catalog;
    this.render();
  }

  setReadOnly(readOnly) {
    this.readOnly = Boolean(readOnly);
    this.render();
  }

  render() {
    const catalog = this.catalog;
    if (!catalog) {
      this.list.innerHTML = '<div class="cv-panel-loading"><span class="cv-spinner" aria-hidden="true"></span>Loading nodes…</div>';
      return;
    }
    const needle = this.query.trim().toLowerCase();
    const categories = [...(catalog.categories || [])];
    for (const spec of catalog.nodes) {
      if (!categories.some((category) => category.id === spec.category)) categories.push({ id: spec.category, label: spec.category });
    }
    const groups = categories.map((category) => {
      const items = catalog.nodes.filter((spec) => spec.category === category.id
        && (!needle || `${spec.title} ${spec.description} ${spec.type}`.toLowerCase().includes(needle)));
      if (!items.length) return "";
      return `<section class="cv-palette-group" aria-label="${escapeHTML(category.label)}">
          <h3 class="eyebrow">${escapeHTML(category.label)}</h3>
          ${items.map((spec) => `
            <button type="button" class="cv-palette-item c-${escapeHTML(spec.category)}" data-cv-type="${escapeHTML(spec.type)}"
              ${this.readOnly ? 'disabled title="This canvas is view only"' : `title="Drag onto the canvas, or click to add"`}>
              <span class="cv-palette-icon">${icon(hasIcon(spec.icon) ? spec.icon : "square", { size: 16 })}</span>
              <span class="cv-palette-text"><strong>${escapeHTML(spec.title)}</strong><small>${escapeHTML(spec.description || "")}</small></span>
            </button>`).join("")}
        </section>`;
    }).join("");
    this.list.innerHTML = groups || `<p class="cv-palette-empty">No node matches “${escapeHTML(this.query.trim())}”.</p>`;
  }

  onListKey(event) {
    const items = [...this.list.querySelectorAll("[data-cv-type]")];
    const index = items.indexOf(document.activeElement);
    if (index < 0) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const next = items[index + (event.key === "ArrowDown" ? 1 : -1)];
      if (next) next.focus();
      else if (event.key === "ArrowUp") this.search.focus();
    }
  }

  onPointerDown(event) {
    const item = event.target.closest("[data-cv-type]");
    if (!item || item.disabled || event.button !== 0) return;
    this.drag = {
      type: item.dataset.cvType,
      item,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      ghost: null,
    };
    window.addEventListener("pointermove", this.move);
    window.addEventListener("pointerup", this.up);
    window.addEventListener("pointercancel", this.up);
  }

  onPointerMove(event) {
    const drag = this.drag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (!drag.ghost) {
      if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < DRAG_THRESHOLD) return;
      const spec = this.catalog?.get(drag.type);
      drag.ghost = document.createElement("div");
      drag.ghost.className = "cv-ghost";
      drag.ghost.innerHTML = `<span class="cv-node-icon c-${escapeHTML(spec?.category || "")}">${icon(hasIcon(spec?.icon) ? spec.icon : "square", { size: 15 })}</span><span>${escapeHTML(spec?.title || drag.type)}</span>`;
      document.body.append(drag.ghost);
      document.body.classList.add("cv-is-placing");
      drag.item.classList.add("is-dragging");
    }
    const inside = this.isInsideCanvas?.(event.clientX, event.clientY);
    drag.ghost.classList.toggle("is-over-canvas", Boolean(inside));
    drag.ghost.style.transform = `translate(${event.clientX + 10}px, ${event.clientY + 10}px)`;
  }

  onPointerUp(event) {
    const drag = this.drag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    this.drag = null;
    window.removeEventListener("pointermove", this.move);
    window.removeEventListener("pointerup", this.up);
    window.removeEventListener("pointercancel", this.up);
    if (!drag.ghost) return;
    drag.ghost.remove();
    document.body.classList.remove("cv-is-placing");
    drag.item.classList.remove("is-dragging");
    this.suppressClick = true;
    setTimeout(() => { this.suppressClick = false; }, 0);
    if (event.type === "pointerup" && this.isInsideCanvas?.(event.clientX, event.clientY)) {
      this.onDropAt?.(drag.type, event.clientX, event.clientY);
    }
  }
}
