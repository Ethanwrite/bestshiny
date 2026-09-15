/**
 * The infinite canvas: pan and zoom, a dotted grid, node cards, bezier edges,
 * selection, marquee, dragging, connecting, menus, keyboard, clipboard, file
 * drops and a minimap.
 *
 * One transformed layer carries the world: an SVG of edges under absolutely
 * positioned cards, and an SVG for the connection being dragged on top.
 * Pointer moves write transforms and path data directly; nothing re-renders
 * per frame. The document lives in the GraphStore; the editor draws what the
 * store emits and owns only view state (selection, the drag in progress).
 */
import { ZOOM_MAX, ZOOM_MIN, connectionCandidates, inputPort, outputPort, parseClipboardText } from "./graph.js";
import { icon } from "./icons.js";
import { NodeCards, dataTypeClass, nodeWidth, portAnchor, HEADER_HEIGHT, PORTS_PAD, PORT_ROW } from "./node-card.js";
import { MOD, PopupMenu, SHIFT, isEditableTarget, prefersReducedMotion } from "./ui.js";

const GRID = 24;
const DRAG_THRESHOLD = 4;
const SNAP_RADIUS = 30;
const EDGE_PAN_ZONE = 36;
const DEFAULT_SIZE = { w: 300, h: 160 };

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

export function edgePath(a, b) {
  const dx = b.x - a.x;
  const bend = clamp(Math.abs(dx) * 0.5 + (dx < 0 ? Math.min(160, Math.abs(b.y - a.y) * 0.25 + 60) : 0), 40, 260);
  return `M ${a.x} ${a.y} C ${a.x + bend} ${a.y}, ${b.x - bend} ${b.y}, ${b.x} ${b.y}`;
}

function edgeMidpoint(a, b) {
  const dx = b.x - a.x;
  const bend = clamp(Math.abs(dx) * 0.5 + (dx < 0 ? Math.min(160, Math.abs(b.y - a.y) * 0.25 + 60) : 0), 40, 260);
  const p1 = { x: a.x + bend, y: a.y };
  const p2 = { x: b.x - bend, y: b.y };
  return {
    x: 0.125 * a.x + 0.375 * p1.x + 0.375 * p2.x + 0.125 * b.x,
    y: 0.125 * a.y + 0.375 * p1.y + 0.375 * p2.y + 0.125 * b.y,
  };
}

const INTERACTIVE = "input, textarea, select, button, a, video, label, [contenteditable], [data-cv-nodrag]";

export class CanvasEditor {
  constructor(host, env) {
    this.host = host;
    this.env = env;
    this.store = null;
    this.selection = new Set();
    this.selectedEdge = null;
    this.sizes = new Map();
    this.edgeEls = new Map();
    this.edgesByNode = new Map();
    this.mode = null;
    this.spaceDown = false;
    this.pointer = { clientX: 0, clientY: 0, inside: false };
    this.candidates = null;
    this.touches = new Map();
    this.frame = 0;
    this.pending = { minimap: false, edgeButton: false };
    this.build();
    this.cards = new NodeCards(this.nodesLayer, env, this);
    this.menu = new PopupMenu(this.root);
    this.minimap = new Minimap(this, this.root.querySelector(".cv-minimap"));
    this.bind();
  }

  /* ================================================================ setup */

  build() {
    this.root = document.createElement("div");
    this.root.className = "cv-editor";
    this.root.innerHTML = `
      <div class="cv-canvas" tabindex="-1" aria-label="Canvas. Drag to pan, ${MOD}+scroll to zoom." role="application">
        <div class="cv-world">
          <svg class="cv-edges" aria-hidden="true" width="1" height="1"><g class="cv-edge-list"></g></svg>
          <div class="cv-nodes"></div>
          <svg class="cv-live" aria-hidden="true" width="1" height="1"><path class="cv-live-path" d=""/></svg>
        </div>
        <div class="cv-marquee" hidden></div>
        <button type="button" class="cv-edge-delete" hidden aria-label="Delete connection" title="Delete connection">${icon("x", { size: 12 })}</button>
        <div class="cv-drop-hint" hidden>${icon("upload", { size: 18 })}<span>Drop images to add them to the canvas</span></div>
      </div>
      <div class="cv-canvas-empty" hidden>
        <strong>An empty canvas</strong>
        <p>Drag a node in from the left, or right-click anywhere to add one.</p>
      </div>
      <div class="cv-zoombar" role="toolbar" aria-label="View">
        <button type="button" class="cv-tool" data-cv-view="undo" title="Undo (${MOD}+Z)" aria-label="Undo">${icon("undo", { size: 15 })}</button>
        <button type="button" class="cv-tool" data-cv-view="redo" title="Redo (${SHIFT}+${MOD}+Z)" aria-label="Redo">${icon("redo", { size: 15 })}</button>
        <span class="cv-tool-sep" aria-hidden="true"></span>
        <button type="button" class="cv-tool" data-cv-view="zoom-out" title="Zoom out" aria-label="Zoom out">${icon("minus", { size: 15 })}</button>
        <span class="cv-zoom-readout mono" aria-live="off">100%</span>
        <button type="button" class="cv-tool" data-cv-view="zoom-in" title="Zoom in" aria-label="Zoom in">${icon("plus", { size: 15 })}</button>
        <span class="cv-tool-sep" aria-hidden="true"></span>
        <button type="button" class="cv-tool" data-cv-view="fit" title="Fit view (F)" aria-label="Fit view">${icon("fit", { size: 15 })}</button>
        <button type="button" class="cv-tool cv-tool-text mono" data-cv-view="actual" title="Zoom to 100%" aria-label="Zoom to 100%">100%</button>
      </div>
      <div class="cv-minimap" aria-label="Minimap. Click to move the view." role="img"><canvas></canvas></div>`;
    this.canvas = this.root.querySelector(".cv-canvas");
    this.world = this.root.querySelector(".cv-world");
    this.edgeList = this.root.querySelector(".cv-edge-list");
    this.nodesLayer = this.root.querySelector(".cv-nodes");
    this.livePath = this.root.querySelector(".cv-live-path");
    this.marquee = this.root.querySelector(".cv-marquee");
    this.edgeDelete = this.root.querySelector(".cv-edge-delete");
    this.dropHint = this.root.querySelector(".cv-drop-hint");
    this.emptyHint = this.root.querySelector(".cv-canvas-empty");
    this.zoomReadout = this.root.querySelector(".cv-zoom-readout");
    this.host.append(this.root);
    this.rect = this.canvas.getBoundingClientRect();
  }

  bind() {
    const canvas = this.canvas;
    this.listeners = [];
    const listen = (target, type, handler, options) => {
      target.addEventListener(type, handler, options);
      this.listeners.push(() => target.removeEventListener(type, handler, options));
    };
    listen(canvas, "pointerdown", (event) => this.onPointerDown(event));
    listen(canvas, "pointermove", (event) => this.onPointerMove(event));
    listen(canvas, "pointerup", (event) => this.onPointerUp(event));
    listen(canvas, "pointercancel", (event) => this.onPointerUp(event, { cancelled: true }));
    listen(canvas, "lostpointercapture", (event) => { if (this.mode?.pointerId === event.pointerId) this.onPointerUp(event, { cancelled: true }); });
    listen(canvas, "pointerenter", () => { this.pointer.inside = true; });
    listen(canvas, "pointerleave", () => { this.pointer.inside = false; this.hideEdgeDelete(); });
    listen(canvas, "wheel", (event) => this.onWheel(event), { passive: false });
    listen(canvas, "contextmenu", (event) => this.onContextMenu(event));
    listen(canvas, "dblclick", (event) => this.onDoubleClick(event));
    listen(canvas, "focusin", (event) => this.onFocusIn(event));
    listen(canvas, "gesturestart", (event) => this.onGesture(event, "start"));
    listen(canvas, "gesturechange", (event) => this.onGesture(event, "change"));
    listen(canvas, "dragenter", (event) => this.onFileDrag(event));
    listen(canvas, "dragover", (event) => this.onFileDrag(event));
    listen(canvas, "dragleave", (event) => this.onFileDragLeave(event));
    listen(canvas, "drop", (event) => this.onFileDrop(event));
    listen(this.edgeList, "pointerover", (event) => this.onEdgeHover(event));
    listen(this.edgeDelete, "click", () => this.deleteHoveredEdge());
    listen(this.edgeDelete, "pointerdown", (event) => event.stopPropagation());
    listen(this.root.querySelector(".cv-zoombar"), "click", (event) => this.onViewButton(event));
    listen(document, "keydown", (event) => this.onKeyDown(event));
    listen(document, "keyup", (event) => this.onKeyUp(event));
    listen(window, "blur", () => this.setSpace(false));
    listen(document, "copy", (event) => this.onCopy(event, false));
    listen(document, "cut", (event) => this.onCopy(event, true));
    listen(document, "paste", (event) => this.onPaste(event));
    this.resizeObserver = new ResizeObserver((entries) => {
      let world = false;
      for (const entry of entries) {
        if (entry.target === this.canvas) {
          this.rect = this.canvas.getBoundingClientRect();
          this.schedule("minimap");
          if (this.pendingFit && this.rect.width >= 120 && this.rect.height >= 120) {
            const options = this.pendingFit;
            this.pendingFit = null;
            requestAnimationFrame(() => this.fitView(options));
          }
          continue;
        }
        const id = entry.target.dataset.cvNode;
        if (!id) continue;
        const box = entry.borderBoxSize?.[0];
        this.sizes.set(id, box ? { w: box.inlineSize, h: box.blockSize } : { w: entry.target.offsetWidth, h: entry.target.offsetHeight });
        world = true;
      }
      if (world) this.schedule("minimap");
    });
    this.resizeObserver.observe(this.canvas);
  }

  destroy() {
    this.detach();
    this.listeners.forEach((off) => off());
    this.resizeObserver.disconnect();
    cancelAnimationFrame(this.frame);
    cancelAnimationFrame(this.autoPanFrame);
    this.menu.destroy();
    this.root.remove();
  }

  /** Draw a (new) store. */
  attach(store) {
    this.detach();
    this.store = store;
    this.env.store = store;
    this.unsubscribe = store.subscribe((change) => this.onStoreChange(change));
    this.renderAll();
    this.applyViewport();
    this.updateHistoryButtons();
  }

  detach() {
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.cancelMode();
    this.menu?.close({ silent: true });
    this.cards?.clear();
    this.edgeList.innerHTML = "";
    this.edgeEls.clear();
    this.edgesByNode.clear();
    this.sizes.clear();
    this.selection.clear();
    this.selectedEdge = null;
    this.store = null;
  }

  isActive() {
    return Boolean(this.store) && this.root.isConnected && this.env.isActive?.() !== false
      && this.root.offsetParent !== null;
  }

  /* ================================================================ store -> view */

  onStoreChange(change) {
    switch (change.kind) {
      case "viewport":
        this.applyViewport();
        return;
      case "add":
        for (const id of change.nodeIds || []) this.createCard(this.store.getNode(id));
        for (const id of change.edgeIds || []) this.createEdge(this.store.getEdge(id));
        this.rebuildEdgeIndex();
        this.cards.refreshPorts(this.neighbours(change.edgeIds || []));
        break;
      case "remove":
        for (const id of change.nodeIds || []) this.removeCard(id);
        for (const id of change.edgeIds || []) this.removeEdge(id);
        this.rebuildEdgeIndex();
        this.cards.refreshPorts(change.touchedNodeIds || []);
        this.pruneSelection();
        break;
      case "move":
        for (const id of change.nodeIds || []) this.cards.sync(this.store.getNode(id));
        this.updateEdgesFor(change.nodeIds || []);
        break;
      case "data":
      case "label":
        for (const id of change.nodeIds || []) this.cards.sync(this.store.getNode(id));
        if (change.kind === "label") this.cards.refreshPorts(this.downstreamTitles(change.nodeIds || []));
        break;
      case "edges":
        for (const id of change.removed || []) this.removeEdge(id);
        for (const id of change.added || []) this.createEdge(this.store.getEdge(id));
        this.rebuildEdgeIndex();
        this.cards.refreshPorts(change.nodeIds || []);
        if (this.selectedEdge && !this.store.getEdge(this.selectedEdge)) this.selectEdge(null);
        break;
      case "replace":
        this.renderAll();
        if (change.origin === "load") this.applyViewport();
        break;
      default:
        break;
    }
    this.updateHistoryButtons();
    this.updateEmptyHint();
    this.schedule("minimap");
  }

  neighbours(edgeIds) {
    const ids = new Set();
    for (const id of edgeIds) {
      const edge = this.store.getEdge(id);
      if (edge) { ids.add(edge.source); ids.add(edge.target); }
    }
    return [...ids];
  }

  /** Nodes whose "Using connected text from X" names a renamed node. */
  downstreamTitles(ids) {
    const renamed = new Set(ids);
    return [...new Set(this.store.edges.filter((edge) => renamed.has(edge.source)).map((edge) => edge.target))];
  }

  renderAll() {
    const store = this.store;
    const seen = new Set();
    const existing = [];
    for (const node of store.nodes) {
      seen.add(node.id);
      if (this.cards.get(node.id)) {
        this.cards.sync(node);
        existing.push(node.id);
      } else {
        this.createCard(node);
      }
    }
    for (const id of this.cards.ids()) if (!seen.has(id)) this.removeCard(id);
    const edgeIds = new Set(store.edges.map((edge) => edge.id));
    for (const id of [...this.edgeEls.keys()]) if (!edgeIds.has(id)) this.removeEdge(id);
    for (const edge of store.edges) {
      if (!this.edgeEls.has(edge.id)) this.createEdge(edge);
    }
    this.rebuildEdgeIndex();
    for (const edge of store.edges) this.updateEdge(edge);
    // New cards were drawn against the current edges; only kept cards may be stale.
    this.cards.refreshPorts(existing);
    this.pruneSelection();
    this.updateEmptyHint();
    this.schedule("minimap");
  }

  createCard(node) {
    if (!node) return;
    const record = this.cards.create(node);
    record.el.classList.toggle("is-selected", this.selection.has(node.id));
    this.resizeObserver.observe(record.el);
  }

  removeCard(id) {
    const record = this.cards.get(id);
    if (record) this.resizeObserver.unobserve(record.el);
    this.cards.remove(id);
    this.sizes.delete(id);
  }

  /* ---------------------------------------------------------------- edges */

  rebuildEdgeIndex() {
    this.edgesByNode.clear();
    for (const edge of this.store.edges) {
      for (const id of [edge.source, edge.target]) {
        if (!this.edgesByNode.has(id)) this.edgesByNode.set(id, []);
        this.edgesByNode.get(id).push(edge.id);
      }
    }
  }

  endpoints(edge) {
    const source = this.store.getNode(edge.source);
    const target = this.store.getNode(edge.target);
    if (!source || !target) return null;
    return {
      a: portAnchor(source, this.store.catalog.get(source.type), "out", edge.source_port),
      b: portAnchor(target, this.store.catalog.get(target.type), "in", edge.target_port),
    };
  }

  edgeDataType(edge) {
    const source = this.store.getNode(edge.source);
    return outputPort(this.store.catalog.get(source?.type), edge.source_port)?.data_type || "other";
  }

  createEdge(edge) {
    if (!edge || this.edgeEls.has(edge.id)) return;
    const ns = "http://www.w3.org/2000/svg";
    const group = document.createElementNS(ns, "g");
    group.setAttribute("class", `cv-edge ${dataTypeClass(this.edgeDataType(edge))}`);
    group.dataset.cvEdgeGroup = edge.id;
    const hit = document.createElementNS(ns, "path");
    hit.setAttribute("class", "cv-edge-hit");
    hit.dataset.cvEdge = edge.id;
    const line = document.createElementNS(ns, "path");
    line.setAttribute("class", "cv-edge-line");
    group.append(hit, line);
    this.edgeList.append(group);
    this.edgeEls.set(edge.id, { group, hit, line });
    if (this.selectedEdge === edge.id) group.classList.add("is-selected");
    this.updateEdge(edge);
  }

  removeEdge(id) {
    const entry = this.edgeEls.get(id);
    if (!entry) return;
    entry.group.remove();
    this.edgeEls.delete(id);
    if (this.hoveredEdge === id) this.hideEdgeDelete();
  }

  updateEdge(edge) {
    const entry = this.edgeEls.get(edge.id);
    const points = this.endpoints(edge);
    if (!entry || !points) return;
    const d = edgePath(points.a, points.b);
    entry.hit.setAttribute("d", d);
    entry.line.setAttribute("d", d);
  }

  updateEdgesFor(nodeIds) {
    const done = new Set();
    for (const id of nodeIds) {
      for (const edgeId of this.edgesByNode.get(id) || []) {
        if (done.has(edgeId)) continue;
        done.add(edgeId);
        const edge = this.store.getEdge(edgeId);
        if (edge) this.updateEdge(edge);
      }
    }
    if (this.hoveredEdge && done.has(this.hoveredEdge)) this.schedule("edgeButton");
  }

  onEdgeHover(event) {
    if (this.mode) return;
    const id = event.target.closest?.("[data-cv-edge]")?.dataset.cvEdge;
    if (!id || this.env.readOnly) return;
    this.hoveredEdge = id;
    this.placeEdgeDelete();
  }

  placeEdgeDelete() {
    const edge = this.hoveredEdge ? this.store?.getEdge(this.hoveredEdge) : null;
    const points = edge ? this.endpoints(edge) : null;
    if (!points) return this.hideEdgeDelete();
    const mid = edgeMidpoint(points.a, points.b);
    const screen = this.worldToCanvas(mid.x, mid.y);
    this.edgeDelete.style.transform = `translate(${screen.x - 11}px, ${screen.y - 11}px)`;
    this.edgeDelete.hidden = false;
    clearTimeout(this.edgeHideTimer);
    this.edgeHideTimer = setTimeout(() => {
      if (!this.edgeDelete.matches(":hover") && !this.edgeEls.get(this.hoveredEdge)?.hit.matches(":hover")) this.hideEdgeDelete();
      else this.placeEdgeDelete();
    }, 1400);
  }

  hideEdgeDelete() {
    this.hoveredEdge = null;
    this.edgeDelete.hidden = true;
  }

  deleteHoveredEdge() {
    if (!this.hoveredEdge || this.env.readOnly) return;
    this.store.removeEdges([this.hoveredEdge]);
    this.hideEdgeDelete();
  }

  /* ================================================================ viewport */

  get viewport() {
    return this.store?.viewport || { x: 0, y: 0, zoom: 1 };
  }

  applyViewport() {
    const { x, y, zoom } = this.viewport;
    this.world.style.transform = `translate(${x}px, ${y}px) scale(${zoom})`;
    let size = GRID * zoom;
    while (size < 12) size *= 2;
    this.canvas.style.backgroundSize = `${size}px ${size}px`;
    this.canvas.style.backgroundPosition = `${x % size}px ${y % size}px`;
    this.canvas.style.setProperty("--cv-zoom", String(zoom));
    this.canvas.classList.toggle("is-far", zoom < 0.45);
    this.zoomReadout.textContent = `${Math.round(zoom * 100)}%`;
    this.markMoving();
    this.schedule("minimap");
    if (this.hoveredEdge) this.schedule("edgeButton");
    if (this.menu.open && !this.menu.pinned) this.menu.close();
  }

  markMoving() {
    this.world.classList.add("is-moving");
    clearTimeout(this.movingTimer);
    this.movingTimer = setTimeout(() => this.world.classList.remove("is-moving"), 160);
  }

  clientToWorld(clientX, clientY) {
    const rect = this.rect;
    const { x, y, zoom } = this.viewport;
    return { x: (clientX - rect.left - x) / zoom, y: (clientY - rect.top - y) / zoom };
  }

  worldToCanvas(worldX, worldY) {
    const { x, y, zoom } = this.viewport;
    return { x: worldX * zoom + x, y: worldY * zoom + y };
  }

  refreshRect() {
    this.rect = this.canvas.getBoundingClientRect();
    return this.rect;
  }

  zoomAt(clientX, clientY, nextZoom) {
    const rect = this.rect;
    const { x, y, zoom } = this.viewport;
    const target = clamp(nextZoom, ZOOM_MIN, ZOOM_MAX);
    const px = clientX - rect.left;
    const py = clientY - rect.top;
    const wx = (px - x) / zoom;
    const wy = (py - y) / zoom;
    this.store.setViewport({ zoom: target, x: px - wx * target, y: py - wy * target });
  }

  zoomBy(factor) {
    this.refreshRect();
    this.animateTo({ ...this.zoomTarget(this.rect.left + this.rect.width / 2, this.rect.top + this.rect.height / 2, this.viewport.zoom * factor) });
  }

  zoomTarget(clientX, clientY, nextZoom) {
    const rect = this.rect;
    const { x, y, zoom } = this.viewport;
    const target = clamp(nextZoom, ZOOM_MIN, ZOOM_MAX);
    const px = clientX - rect.left;
    const py = clientY - rect.top;
    return { zoom: target, x: px - ((px - x) / zoom) * target, y: py - ((py - y) / zoom) * target };
  }

  animateTo(target, duration = 200) {
    if (!this.store) return;
    cancelAnimationFrame(this.viewAnimation);
    if (prefersReducedMotion() || duration <= 0) {
      this.store.setViewport(target);
      return;
    }
    const from = { ...this.viewport };
    const started = performance.now();
    const step = (now) => {
      if (!this.store) return;
      const t = Math.min(1, Math.max(0, (now - started) / duration));
      const ease = 1 - (1 - t) ** 3;
      this.store.setViewport({
        x: from.x + (target.x - from.x) * ease,
        y: from.y + (target.y - from.y) * ease,
        zoom: from.zoom + (target.zoom - from.zoom) * ease,
      });
      if (t < 1) this.viewAnimation = requestAnimationFrame(step);
    };
    this.viewAnimation = requestAnimationFrame(step);
  }

  nodeBox(node) {
    let size = this.sizes.get(node.id);
    if (!size) {
      // Not reported by the ResizeObserver yet (a card created this frame): measure it once.
      const el = this.cards.get(node.id)?.el;
      size = el?.isConnected && el.offsetHeight
        ? { w: el.offsetWidth, h: el.offsetHeight }
        : { w: nodeWidth(node), h: DEFAULT_SIZE.h };
      if (el?.offsetHeight) this.sizes.set(node.id, size);
    }
    return { x: node.position.x, y: node.position.y, w: size.w, h: size.h };
  }

  bounds(nodes) {
    if (!nodes.length) return null;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of nodes) {
      const box = this.nodeBox(node);
      minX = Math.min(minX, box.x);
      minY = Math.min(minY, box.y);
      maxX = Math.max(maxX, box.x + box.w);
      maxY = Math.max(maxY, box.y + box.h);
    }
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }

  fitView({ nodeIds = null, padding = 72, maxZoom = 1, animate = true } = {}) {
    if (!this.store) return;
    this.refreshRect();
    if (this.rect.width < 120 || this.rect.height < 120) {
      // The canvas has no real size yet (a hidden or collapsing panel):
      // fitting now would save a nonsense zoom. Fit once it is laid out.
      this.pendingFit = { nodeIds, padding, maxZoom, animate: false };
      return;
    }
    this.pendingFit = null;
    const nodes = nodeIds ? nodeIds.map((id) => this.store.getNode(id)).filter(Boolean) : this.store.nodes;
    const box = this.bounds(nodes);
    const width = Math.max(200, this.rect.width);
    const height = Math.max(200, this.rect.height);
    let target;
    if (!box) {
      target = { x: width / 2 - 150, y: height / 2 - 100, zoom: 1 };
    } else {
      const zoom = clamp(Math.min((width - padding * 2) / Math.max(box.w, 1), (height - padding * 2) / Math.max(box.h, 1)), ZOOM_MIN, maxZoom);
      target = { zoom, x: width / 2 - (box.x + box.w / 2) * zoom, y: height / 2 - (box.y + box.h / 2) * zoom };
    }
    if (animate) this.animateTo(target);
    else this.store.setViewport(target);
  }

  centerOnWorld(x, y, { animate = false } = {}) {
    this.refreshRect();
    const { zoom } = this.viewport;
    const target = { zoom, x: this.rect.width / 2 - x * zoom, y: this.rect.height / 2 - y * zoom };
    if (animate) this.animateTo(target);
    else this.store.setViewport(target);
  }

  /** Scroll a node into view if any part of it is off screen. */
  revealNode(id, { select = true } = {}) {
    const node = this.store?.getNode(id);
    if (!node) return;
    if (select) this.select([id]);
    this.refreshRect();
    const box = this.nodeBox(node);
    const topLeft = this.worldToCanvas(box.x, box.y);
    const { zoom } = this.viewport;
    const inside = topLeft.x >= 16 && topLeft.y >= 16
      && topLeft.x + box.w * zoom <= this.rect.width - 16 && topLeft.y + box.h * zoom <= this.rect.height - 16;
    if (!inside) this.centerOnWorld(box.x + box.w / 2, box.y + Math.min(box.h, 400) / 2, { animate: true });
  }

  viewCenterWorld() {
    this.refreshRect();
    return this.clientToWorld(this.rect.left + this.rect.width / 2, this.rect.top + this.rect.height / 2);
  }

  onViewButton(event) {
    const button = event.target.closest("[data-cv-view]");
    if (!button || !this.store) return;
    const action = button.dataset.cvView;
    if (action === "zoom-in") this.zoomBy(1.25);
    else if (action === "zoom-out") this.zoomBy(0.8);
    else if (action === "fit") this.fitView();
    else if (action === "actual") this.zoomBy(1 / this.viewport.zoom);
    else if (action === "undo") this.store.undo();
    else if (action === "redo") this.store.redo();
  }

  updateHistoryButtons() {
    const undo = this.root.querySelector('[data-cv-view="undo"]');
    const redo = this.root.querySelector('[data-cv-view="redo"]');
    if (undo) undo.disabled = !this.store?.canUndo || this.env.readOnly;
    if (redo) redo.disabled = !this.store?.canRedo || this.env.readOnly;
  }

  updateEmptyHint() {
    this.emptyHint.hidden = !this.store || this.store.nodes.length > 0 || this.env.readOnly;
  }

  /* ================================================================ frames */

  schedule(what) {
    this.pending[what] = true;
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      if (this.pending.minimap) { this.pending.minimap = false; this.minimap.draw(); }
      if (this.pending.edgeButton) { this.pending.edgeButton = false; this.placeEdgeDelete(); }
    });
  }

  /* ================================================================ selection */

  select(ids, { additive = false } = {}) {
    const next = additive ? new Set([...this.selection, ...ids]) : new Set(ids);
    this.setSelection(next);
    if (this.selectedEdge) this.selectEdge(null, { silent: true });
  }

  toggle(id) {
    const next = new Set(this.selection);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this.setSelection(next);
  }

  clearSelection() {
    const changed = this.selection.size > 0 || this.selectedEdge;
    this.setSelection(new Set(), { silent: true });
    this.selectEdge(null, { silent: true });
    if (changed) this.emitSelection();
  }

  selectAll() {
    this.setSelection(new Set(this.store.nodes.map((node) => node.id)));
  }

  setSelection(next, { silent = false } = {}) {
    const before = this.selection;
    for (const id of before) if (!next.has(id)) this.cards.get(id)?.el.classList.remove("is-selected");
    for (const id of next) if (!before.has(id)) this.cards.get(id)?.el.classList.add("is-selected");
    const changed = before.size !== next.size || [...next].some((id) => !before.has(id));
    this.selection = next;
    if (changed && !silent) this.emitSelection();
    if (changed) this.schedule("minimap");
  }

  selectEdge(id, { silent = false } = {}) {
    if (this.selectedEdge === id) return;
    if (this.selectedEdge) this.edgeEls.get(this.selectedEdge)?.group.classList.remove("is-selected");
    this.selectedEdge = id;
    if (id) {
      const entry = this.edgeEls.get(id);
      entry?.group.classList.add("is-selected");
      entry?.group.parentNode?.append(entry.group);
      this.setSelection(new Set(), { silent: true });
    }
    if (!silent) this.emitSelection();
  }

  pruneSelection() {
    const next = new Set([...this.selection].filter((id) => this.store.getNode(id)));
    if (next.size !== this.selection.size) this.setSelection(next);
    if (this.selectedEdge && !this.store.getEdge(this.selectedEdge)) this.selectEdge(null);
  }

  emitSelection() {
    this.env.onSelectionChange?.({ nodeIds: [...this.selection], edgeId: this.selectedEdge });
  }

  onFocusIn(event) {
    // Tabbing onto a card, or into one of its fields, selects that node.
    const card = event.target.closest?.(".cv-node");
    if (!card || this.mode || this.focusFromPointer) return;
    const id = card.dataset.cvNode;
    if (!this.selection.has(id)) this.select([id]);
  }

  /* ================================================================ pointer */

  onPointerDown(event) {
    if (!this.store) return;
    this.pointer.clientX = event.clientX;
    this.pointer.clientY = event.clientY;
    this.refreshRect();
    if (event.pointerType === "touch") {
      this.touches.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (this.touches.size === 2) {
        this.cancelMode();
        this.beginPinch();
        return;
      }
    }
    if (event.button === 2) return;
    const target = event.target;
    if (event.button === 1 || (this.spaceDown && event.button === 0)) {
      event.preventDefault();
      this.beginPan(event, { clearOnClick: false });
      return;
    }
    if (event.button !== 0) return;
    const port = target.closest("[data-cv-port]");
    if (port) {
      event.preventDefault();
      if (!this.env.readOnly) this.beginConnect(event, port);
      return;
    }
    const edgeHit = target.closest("[data-cv-edge]");
    if (edgeHit) {
      event.preventDefault();
      this.selectEdge(edgeHit.dataset.cvEdge);
      this.canvas.focus({ preventScroll: true });
      return;
    }
    const card = target.closest(".cv-node");
    if (card) {
      const id = card.dataset.cvNode;
      if (target.closest(INTERACTIVE)) {
        if (!this.selection.has(id) && !(event.shiftKey || event.metaKey || event.ctrlKey)) this.select([id]);
        return;
      }
      event.preventDefault();
      this.beginNodeDrag(event, id, card);
      return;
    }
    if (target === this.canvas || target === this.world || target.closest(".cv-edges, .cv-live")) {
      event.preventDefault();
      if (document.activeElement && this.root.contains(document.activeElement)) document.activeElement.blur();
      this.canvas.focus({ preventScroll: true });
      if (event.shiftKey) this.beginMarquee(event);
      else this.beginPan(event, { clearOnClick: true });
    }
  }

  capture(event) {
    try { this.canvas.setPointerCapture(event.pointerId); } catch (_error) { /* synthetic pointer */ }
  }

  onPointerMove(event) {
    this.pointer.clientX = event.clientX;
    this.pointer.clientY = event.clientY;
    if (event.pointerType === "touch" && this.touches.has(event.pointerId)) {
      this.touches.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (this.mode?.type === "pinch") {
        this.updatePinch();
        return;
      }
    }
    const mode = this.mode;
    if (!mode || mode.pointerId !== event.pointerId) return;
    if (mode.type === "pan") this.updatePan(event);
    else if (mode.type === "drag") this.updateNodeDrag(event);
    else if (mode.type === "marquee") this.updateMarquee(event);
    else if (mode.type === "connect") this.updateConnect(event);
  }

  onPointerUp(event, { cancelled = false } = {}) {
    if (event.pointerType === "touch") {
      this.touches.delete(event.pointerId);
      if (this.mode?.type === "pinch") {
        if (this.touches.size < 2) this.mode = null;
        return;
      }
    }
    const mode = this.mode;
    if (!mode || mode.pointerId !== event.pointerId) return;
    this.mode = null;
    try { this.canvas.releasePointerCapture(event.pointerId); } catch (_error) { /* not captured */ }
    cancelAnimationFrame(this.autoPanFrame);
    this.autoPanFrame = 0;
    if (mode.type === "pan") this.endPan(mode);
    else if (mode.type === "drag") this.endNodeDrag(mode, cancelled);
    else if (mode.type === "marquee") this.endMarquee(mode);
    else if (mode.type === "connect") this.endConnect(mode, event, cancelled);
  }

  cancelMode() {
    const mode = this.mode;
    if (!mode) return;
    this.mode = null;
    cancelAnimationFrame(this.autoPanFrame);
    this.autoPanFrame = 0;
    if (mode.type === "drag") this.endNodeDrag(mode, true);
    else if (mode.type === "marquee") this.endMarquee(mode);
    else if (mode.type === "connect") this.endConnect(mode, null, true);
    else if (mode.type === "pan") this.endPan(mode);
  }

  /* ---- pan ---- */

  beginPan(event, { clearOnClick }) {
    this.mode = {
      type: "pan", pointerId: event.pointerId, startX: event.clientX, startY: event.clientY,
      origin: { ...this.viewport }, moved: false, clearOnClick,
    };
    this.capture(event);
  }

  updatePan(event) {
    const mode = this.mode;
    const dx = event.clientX - mode.startX;
    const dy = event.clientY - mode.startY;
    if (!mode.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    if (!mode.moved) {
      mode.moved = true;
      this.canvas.classList.add("is-panning");
      this.menu.close();
    }
    this.store.setViewport({ x: mode.origin.x + dx, y: mode.origin.y + dy });
  }

  endPan(mode) {
    this.canvas.classList.remove("is-panning");
    if (!mode.moved && mode.clearOnClick) {
      this.menu.close();
      this.clearSelection();
    }
  }

  /* ---- pinch (touch) ---- */

  beginPinch() {
    const [a, b] = [...this.touches.values()];
    this.mode = {
      type: "pinch",
      distance: Math.hypot(a.x - b.x, a.y - b.y) || 1,
      mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
      origin: { ...this.viewport },
    };
  }

  updatePinch() {
    const mode = this.mode;
    const [a, b] = [...this.touches.values()];
    if (!a || !b) return;
    const distance = Math.hypot(a.x - b.x, a.y - b.y) || 1;
    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    const zoom = clamp(mode.origin.zoom * (distance / mode.distance), ZOOM_MIN, ZOOM_MAX);
    const rect = this.rect;
    const wx = (mode.mid.x - rect.left - mode.origin.x) / mode.origin.zoom;
    const wy = (mode.mid.y - rect.top - mode.origin.y) / mode.origin.zoom;
    this.store.setViewport({ zoom, x: mid.x - rect.left - wx * zoom, y: mid.y - rect.top - wy * zoom });
  }

  /* ---- node drag ---- */

  beginNodeDrag(event, id, card) {
    const additive = event.shiftKey || event.metaKey || event.ctrlKey;
    if (document.activeElement && this.root.contains(document.activeElement) && document.activeElement !== card) {
      document.activeElement.blur();
    }
    this.focusFromPointer = true;
    card.focus({ preventScroll: true });
    this.focusFromPointer = false;
    if (additive) {
      this.toggle(id);
      if (!this.selection.has(id)) return;
    } else if (!this.selection.has(id)) {
      this.select([id]);
    }
    if (this.env.readOnly) return;
    const ids = [...this.selection].filter((nodeId) => this.store.getNode(nodeId));
    this.mode = {
      type: "drag",
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startWorld: this.clientToWorld(event.clientX, event.clientY),
      origins: new Map(ids.map((nodeId) => [nodeId, { ...this.store.getNode(nodeId).position }])),
      moved: false,
      clickedId: id,
    };
    this.capture(event);
  }

  updateNodeDrag(event) {
    const mode = this.mode;
    if (!mode.moved && Math.hypot(event.clientX - mode.startX, event.clientY - mode.startY) < DRAG_THRESHOLD) return;
    if (!mode.moved) {
      mode.moved = true;
      this.store.beginBatch();
      this.canvas.classList.add("is-dragging");
      for (const id of mode.origins.keys()) this.cards.get(id)?.el.classList.add("is-dragging");
      this.menu.close();
    }
    this.applyNodeDrag();
    this.autoPan();
  }

  applyNodeDrag() {
    const mode = this.mode;
    if (!mode || mode.type !== "drag") return;
    const now = this.clientToWorld(this.pointer.clientX, this.pointer.clientY);
    const dx = now.x - mode.startWorld.x;
    const dy = now.y - mode.startWorld.y;
    const positions = [];
    for (const [id, origin] of mode.origins) positions.push({ id, x: origin.x + dx, y: origin.y + dy });
    this.store.setPositions(positions);
  }

  endNodeDrag(mode, cancelled) {
    this.canvas.classList.remove("is-dragging");
    for (const id of mode.origins.keys()) this.cards.get(id)?.el.classList.remove("is-dragging");
    if (!mode.moved) return;
    if (cancelled) this.store.cancelBatch();
    else this.store.endBatch();
  }

  /** While dragging near an edge of the canvas, move the view. */
  autoPan() {
    if (this.autoPanFrame) return;
    const step = () => {
      this.autoPanFrame = 0;
      const mode = this.mode;
      if (!mode || (mode.type !== "drag" && mode.type !== "connect")) return;
      const rect = this.rect;
      const { clientX, clientY } = this.pointer;
      const push = (distance) => (distance < EDGE_PAN_ZONE ? (EDGE_PAN_ZONE - Math.max(0, distance)) / EDGE_PAN_ZONE : 0);
      const vx = push(clientX - rect.left) - push(rect.right - clientX);
      const vy = push(clientY - rect.top) - push(rect.bottom - clientY);
      if (!vx && !vy) return;
      const speed = 14;
      this.store.setViewport({ x: this.viewport.x + vx * speed, y: this.viewport.y + vy * speed });
      if (mode.type === "drag") this.applyNodeDrag();
      else this.updateConnect(null);
      this.autoPanFrame = requestAnimationFrame(step);
    };
    this.autoPanFrame = requestAnimationFrame(step);
  }

  /* ---- marquee ---- */

  beginMarquee(event) {
    this.mode = {
      type: "marquee", pointerId: event.pointerId, startX: event.clientX, startY: event.clientY,
      base: new Set(this.selection),
    };
    this.capture(event);
    this.marquee.hidden = false;
    this.updateMarquee(event);
  }

  updateMarquee(event) {
    const mode = this.mode;
    const rect = this.rect;
    const x1 = Math.min(mode.startX, event.clientX) - rect.left;
    const y1 = Math.min(mode.startY, event.clientY) - rect.top;
    const x2 = Math.max(mode.startX, event.clientX) - rect.left;
    const y2 = Math.max(mode.startY, event.clientY) - rect.top;
    Object.assign(this.marquee.style, {
      transform: `translate(${x1}px, ${y1}px)`, width: `${x2 - x1}px`, height: `${y2 - y1}px`,
    });
    const { x, y, zoom } = this.viewport;
    const world = { x1: (x1 - x) / zoom, y1: (y1 - y) / zoom, x2: (x2 - x) / zoom, y2: (y2 - y) / zoom };
    const next = new Set(mode.base);
    for (const node of this.store.nodes) {
      const box = this.nodeBox(node);
      if (box.x < world.x2 && box.x + box.w > world.x1 && box.y < world.y2 && box.y + box.h > world.y1) next.add(node.id);
    }
    this.setSelection(next, { silent: true });
  }

  endMarquee() {
    this.marquee.hidden = true;
    this.emitSelection();
  }

  /* ---- connect ---- */

  beginConnect(event, portEl) {
    const card = portEl.closest(".cv-node");
    const nodeId = card.dataset.cvNode;
    const key = portEl.dataset.cvPortKey;
    const direction = portEl.dataset.cvPort;
    let from = { nodeId, port: key, direction: direction === "in" ? "in" : "out" };
    let batch = false;
    if (from.direction === "in") {
      const existing = this.store.incoming(nodeId, key);
      if (existing.length) {
        // Pick up the newest edge on this input and carry it by its source.
        const edge = existing[existing.length - 1];
        this.store.beginBatch();
        batch = true;
        this.store.removeEdges([edge.id]);
        from = { nodeId: edge.source, port: edge.source_port, direction: "out", detached: true };
      }
    }
    const node = this.store.getNode(from.nodeId);
    const spec = this.store.catalog.get(node?.type);
    const port = from.direction === "out" ? outputPort(spec, from.port) : inputPort(spec, from.port);
    if (!node || !port) {
      if (batch) this.store.endBatch();
      return;
    }
    from.dataType = port.data_type;
    this.connectFrom = from;
    this.candidates = connectionCandidates(this.store.doc, this.store.catalog, from);
    this.canvas.classList.add("is-connecting");
    this.livePath.setAttribute("class", `cv-live-path ${dataTypeClass(port.data_type)}`);
    for (const record of this.cards.records.values()) this.paintCandidates(record);
    this.cards.get(from.nodeId)?.el.querySelector(`[data-cv-port="${from.direction}"][data-cv-port-key="${CSS.escape(from.port)}"]`)?.classList.add("is-origin");
    this.mode = {
      type: "connect", pointerId: event.pointerId, from, batch,
      startX: event.clientX, startY: event.clientY, moved: false,
      anchor: portAnchor(node, spec, from.direction, from.port), target: null,
    };
    this.capture(event);
    this.hideEdgeDelete();
    this.updateConnect(event);
  }

  paintCandidates(record) {
    const from = this.connectFrom || null;
    const candidates = this.candidates;
    const el = record.el;
    if (!candidates) {
      el.classList.remove("is-no-candidate", "is-drop-target");
      el.querySelectorAll(".cv-port").forEach((port) => port.classList.remove("is-compatible", "is-incompatible", "is-origin", "is-hot"));
      return;
    }
    let any = false;
    const opposite = from?.direction === "in" ? "out" : "in";
    el.querySelectorAll(".cv-port").forEach((portEl) => {
      if (portEl.dataset.cvPort !== opposite) {
        portEl.classList.add("is-incompatible");
        return;
      }
      const verdict = candidates.get(`${record.id}:${portEl.dataset.cvPortKey}`);
      portEl.classList.toggle("is-compatible", Boolean(verdict?.ok));
      portEl.classList.toggle("is-incompatible", !verdict?.ok);
      if (verdict?.ok) any = true;
    });
    el.classList.toggle("is-no-candidate", !any);
  }

  /** The best place to drop the connection under the pointer. */
  connectTarget(clientX, clientY, mode = this.mode) {
    if (!mode || mode.type !== "connect") return null;
    const world = this.clientToWorld(clientX, clientY);
    const opposite = mode.from.direction === "out" ? "in" : "out";
    const radius = SNAP_RADIUS / this.viewport.zoom;
    let best = null;
    for (const [key, verdict] of this.candidates) {
      if (!verdict.ok) continue;
      const split = key.lastIndexOf(":");
      const nodeId = key.slice(0, split);
      const portKey = key.slice(split + 1);
      const node = this.store.getNode(nodeId);
      if (!node) continue;
      const anchor = portAnchor(node, this.store.catalog.get(node.type), opposite, portKey);
      const distance = Math.hypot(anchor.x - world.x, anchor.y - world.y);
      if (distance <= radius && (!best || distance < best.distance)) {
        best = { kind: "port", nodeId, portKey, anchor, distance };
      }
    }
    if (best) return best;
    const under = document.elementFromPoint(clientX, clientY);
    const card = under?.closest?.(".cv-node");
    if (card && this.nodesLayer.contains(card)) {
      const nodeId = card.dataset.cvNode;
      const spec = this.store.catalog.get(this.store.getNode(nodeId)?.type);
      const ports = opposite === "in" ? spec?.inputs || [] : spec?.outputs || [];
      const verdicts = ports.map((port) => ({ port, verdict: this.candidates.get(`${nodeId}:${port.key}`) }))
        .filter((item) => item.verdict?.ok);
      const free = verdicts.find((item) => !item.verdict.replaces) || verdicts[0];
      if (free) {
        const node = this.store.getNode(nodeId);
        return { kind: "node", nodeId, portKey: free.port.key, anchor: portAnchor(node, spec, opposite, free.port.key) };
      }
      return { kind: "refused", nodeId };
    }
    if (under === this.canvas || under?.closest?.(".cv-edges, .cv-live") || under === this.world) {
      return { kind: "empty", world };
    }
    return null;
  }

  updateConnect(event) {
    const mode = this.mode;
    if (!mode || mode.type !== "connect") return;
    const clientX = event ? event.clientX : this.pointer.clientX;
    const clientY = event ? event.clientY : this.pointer.clientY;
    if (!mode.moved && Math.hypot(clientX - mode.startX, clientY - mode.startY) >= DRAG_THRESHOLD) mode.moved = true;
    const target = this.connectTarget(clientX, clientY);
    const previous = mode.target;
    if (previous?.nodeId !== target?.nodeId || previous?.portKey !== target?.portKey) {
      if (previous?.nodeId) {
        const record = this.cards.get(previous.nodeId);
        record?.el.classList.remove("is-drop-target");
        record?.el.querySelectorAll(".cv-port.is-hot").forEach((port) => port.classList.remove("is-hot"));
      }
      if (target?.nodeId && target.kind !== "refused") {
        const record = this.cards.get(target.nodeId);
        record?.el.classList.add("is-drop-target");
        const dir = mode.from.direction === "out" ? "in" : "out";
        record?.el.querySelector(`[data-cv-port="${dir}"][data-cv-port-key="${CSS.escape(target.portKey)}"]`)?.classList.add("is-hot");
      }
    }
    mode.target = target;
    const pointerWorld = this.clientToWorld(clientX, clientY);
    const end = target?.anchor || pointerWorld;
    const [a, b] = mode.from.direction === "out" ? [mode.anchor, end] : [end, mode.anchor];
    this.livePath.setAttribute("d", edgePath(a, b));
    this.livePath.classList.toggle("is-snapped", Boolean(target?.anchor));
    if (event) this.autoPan();
  }

  endConnect(mode, event, cancelled) {
    const clicked = !mode.moved;
    const target = cancelled || clicked ? null : (event ? this.connectTarget(event.clientX, event.clientY, mode) : mode.target);
    if (mode.target?.nodeId) this.cards.get(mode.target.nodeId)?.el.classList.remove("is-drop-target");
    this.canvas.classList.remove("is-connecting");
    this.livePath.setAttribute("d", "");
    this.candidates = null;
    this.connectFrom = null;
    for (const record of this.cards.records.values()) this.paintCandidates(record);
    const from = mode.from;
    if (cancelled || clicked) {
      // A click on a port, or Escape, changes nothing - not even a detached edge.
      if (mode.batch) this.store.cancelBatch();
      return;
    }
    const complete = (nodeId, portKey) => {
      const candidate = from.direction === "out"
        ? { source: from.nodeId, source_port: from.port, target: nodeId, target_port: portKey }
        : { source: nodeId, source_port: portKey, target: from.nodeId, target_port: from.port };
      const result = this.store.connect(candidate);
      if (!result.ok) this.env.actions.toast?.(result.message);
    };
    if (target?.kind === "port" || target?.kind === "node") {
      complete(target.nodeId, target.portKey);
      if (mode.batch) this.store.endBatch();
      return;
    }
    if (mode.batch) this.store.endBatch();
    if (target?.kind === "refused") {
      this.env.actions.toast?.("Nothing on that node accepts this connection.");
      return;
    }
    if (target?.kind === "empty" && event) {
      this.openQuickAdd(from, event.clientX, event.clientY);
    }
  }

  /* ================================================================ menus */

  canvasPoint(clientX, clientY) {
    return { x: clientX - this.rect.left, y: clientY - this.rect.top };
  }

  typeItems({ at, filter = null, onCreate = null }) {
    const catalog = this.store.catalog;
    const categories = new Map((catalog.categories || []).map((category) => [category.id, category.label]));
    return catalog.nodes
      .filter((spec) => !filter || filter(spec))
      .sort((a, b) => {
        const order = (catalog.categories || []).map((category) => category.id);
        return (order.indexOf(a.category) - order.indexOf(b.category)) || 0;
      })
      .map((spec) => ({
        label: spec.title,
        description: spec.description,
        icon: spec.icon,
        group: categories.get(spec.category) || spec.category,
        keywords: spec.type,
        onSelect: () => {
          if (!this.store) return;
          const store = this.store;
          store.beginBatch();
          try {
            const node = this.addNodeAt(spec.type, at);
            if (node) onCreate?.(node, spec);
          } finally {
            store.endBatch();
          }
        },
      }));
  }

  openAddMenu(clientX, clientY) {
    if (this.env.readOnly) return;
    const world = this.clientToWorld(clientX, clientY);
    const point = this.canvasPoint(clientX, clientY);
    const items = this.typeItems({ at: { x: world.x - 20, y: world.y - 18 } });
    if (this.env.clipboard?.()) {
      items.push({ separator: true }, {
        label: "Paste", icon: "copy", shortcut: `${MOD}V`, onSelect: () => this.pasteClipboard({ at: world }),
      });
    }
    this.menu.show({ x: point.x, y: point.y, items, title: "Add a node", search: { placeholder: "Search nodes" }, minWidth: 260 });
  }

  openQuickAdd(from, clientX, clientY) {
    if (this.env.readOnly) return;
    const world = this.clientToWorld(clientX, clientY);
    const point = this.canvasPoint(clientX, clientY);
    const wantsInput = from.direction === "out";
    const typeLabel = this.store.catalog.dataTypes?.[from.dataType]?.label || from.dataType;
    const items = this.typeItems({
      at: world,
      filter: (spec) => (wantsInput ? spec.inputs : spec.outputs).some((port) => port.data_type === from.dataType),
      onCreate: (node, spec) => {
        const ports = wantsInput ? spec.inputs : spec.outputs;
        const candidates = ports.filter((port) => port.data_type === from.dataType);
        const store = this.store;
        const port = candidates.find((item) => item.key === "prompt") || candidates[0];
        const offsetY = HEADER_HEIGHT + PORTS_PAD + ports.indexOf(port) * PORT_ROW + PORT_ROW / 2;
        const x = wantsInput ? world.x + 28 : world.x - nodeWidth(node) - 28;
        store.setPositions([{ id: node.id, x, y: world.y - offsetY }]);
        const candidate = wantsInput
          ? { source: from.nodeId, source_port: from.port, target: node.id, target_port: port.key }
          : { source: node.id, source_port: port.key, target: from.nodeId, target_port: from.port };
        const result = store.connect(candidate);
        if (!result.ok) this.env.actions.toast?.(result.message);
      },
    });
    this.menu.show({
      x: point.x, y: point.y, items, minWidth: 260,
      title: wantsInput ? `Add a node that takes ${String(typeLabel).toLowerCase()}` : `Add a node that makes ${String(typeLabel).toLowerCase()}`,
      search: { placeholder: "Search nodes" },
    });
  }

  nodeMenuItems(id) {
    const node = this.store.getNode(id);
    if (!node) return [];
    const spec = this.store.catalog.get(node.type);
    const ids = this.selection.has(id) && this.selection.size > 1 ? [...this.selection] : [id];
    const many = ids.length > 1;
    const readOnly = this.env.readOnly;
    const runnable = ids.filter((nodeId) => this.store.catalog.get(this.store.getNode(nodeId)?.type)?.executable);
    const items = [];
    items.push({
      label: many ? `Run ${runnable.length} selected` : "Run this node",
      icon: "play",
      disabled: readOnly || !runnable.length || this.env.runActive,
      onSelect: () => this.env.actions.runNodes?.(runnable),
    });
    if (!many && spec) {
      items.push({ label: "Rename", icon: "pencil", disabled: readOnly, onSelect: () => this.cards.startRename(id) });
    }
    items.push(
      { separator: true },
      { label: many ? `Duplicate ${ids.length} nodes` : "Duplicate", icon: "duplicate", shortcut: `${MOD}D`, disabled: readOnly, onSelect: () => this.duplicate(ids) },
      { label: "Copy", icon: "copy", shortcut: `${MOD}C`, onSelect: () => this.copyNodes(ids) },
      { separator: true },
      { label: many ? `Delete ${ids.length} nodes` : "Delete", icon: "trash", shortcut: "⌫", danger: true, disabled: readOnly, onSelect: () => this.store.removeNodes(ids) },
    );
    return items;
  }

  openNodeMenu(id, anchor) {
    if (!this.selection.has(id)) this.select([id]);
    const rect = anchor.getBoundingClientRect();
    this.refreshRect();
    const point = this.canvasPoint(rect.right - 220, rect.bottom + 4);
    this.menu.show({ x: point.x, y: point.y, items: this.nodeMenuItems(id), anchor, minWidth: 220 });
  }

  onContextMenu(event) {
    if (!this.store) return;
    if (isEditableTarget(event.target)) return;
    event.preventDefault();
    this.refreshRect();
    const card = event.target.closest(".cv-node");
    const point = this.canvasPoint(event.clientX, event.clientY);
    if (card) {
      const id = card.dataset.cvNode;
      if (!this.selection.has(id)) this.select([id]);
      this.menu.show({ x: point.x, y: point.y, items: this.nodeMenuItems(id) });
      return;
    }
    const edgeHit = event.target.closest("[data-cv-edge]");
    if (edgeHit) {
      const id = edgeHit.dataset.cvEdge;
      this.selectEdge(id);
      this.menu.show({
        x: point.x, y: point.y,
        items: [{ label: "Delete connection", icon: "trash", danger: true, disabled: this.env.readOnly, onSelect: () => this.store.removeEdges([id]) }],
      });
      return;
    }
    this.openAddMenu(event.clientX, event.clientY);
  }

  onDoubleClick(event) {
    if (event.target === this.canvas && !this.env.readOnly) this.openAddMenu(event.clientX, event.clientY);
  }

  /* ================================================================ node commands */

  /** Add a node with its top-left corner at a world point, select it. */
  addNodeAt(type, at, data = {}) {
    if (this.env.readOnly || !this.store) return null;
    const node = this.store.addNode(type, at, data);
    if (!node) {
      this.env.actions.toast?.(this.store.lastError || "That node could not be added.");
      return null;
    }
    this.select([node.id]);
    return node;
  }

  addNodeAtClient(type, clientX, clientY) {
    this.refreshRect();
    const world = this.clientToWorld(clientX, clientY);
    return this.addNodeAt(type, { x: world.x - 24, y: world.y - 20 });
  }

  addNodeAtCenter(type, data = {}) {
    const center = this.viewCenterWorld();
    let at = { x: center.x - 150, y: center.y - 90 };
    const occupied = (point) => this.store.nodes.some((node) => Math.abs(node.position.x - point.x) < 12 && Math.abs(node.position.y - point.y) < 12);
    for (let tries = 0; tries < 20 && occupied(at); tries += 1) at = { x: at.x + 32, y: at.y + 32 };
    const node = this.addNodeAt(type, at, data);
    if (node) requestAnimationFrame(() => this.revealNode(node.id, { select: false }));
    return node;
  }

  isClientInside(clientX, clientY) {
    this.refreshRect();
    const rect = this.rect;
    return clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom;
  }

  deleteSelection() {
    if (this.env.readOnly) return;
    if (this.selection.size) this.store.removeNodes([...this.selection]);
    else if (this.selectedEdge) this.store.removeEdges([this.selectedEdge]);
  }

  duplicate(ids = [...this.selection]) {
    if (this.env.readOnly || !ids.length) return;
    const created = this.store.duplicate(ids, { x: 40, y: 40 });
    if (!created.length && this.store.lastError) this.env.actions.toast?.(this.store.lastError);
    if (created.length) this.select(created);
  }

  copyNodes(ids = [...this.selection]) {
    const payload = this.store.copy(ids);
    if (!payload) return null;
    this.env.setClipboard?.(payload);
    const text = JSON.stringify(payload);
    navigator.clipboard?.writeText?.(text).catch(() => null);
    return payload;
  }

  pasteClipboard({ at = null, payload = null } = {}) {
    if (this.env.readOnly) return;
    const source = payload || this.env.clipboard?.();
    if (!source) return;
    const created = at ? this.store.paste(source, { at }) : this.store.paste(source, { offset: { x: 40, y: 40 } });
    if (!created.length) {
      if (this.store.lastError) this.env.actions.toast?.(this.store.lastError);
      return;
    }
    this.env.setClipboard?.(source);
    this.select(created);
  }

  nudge(dx, dy) {
    if (this.env.readOnly || !this.selection.size) return;
    this.store.moveNodes([...this.selection], dx, dy);
  }

  /* ================================================================ keyboard */

  modalOpen() {
    return Boolean(document.querySelector("dialog[open]"));
  }

  onKeyDown(event) {
    if (!this.isActive() || this.modalOpen()) return;
    if (this.menu.open && this.menu.contains(event.target)) return;
    const mod = event.metaKey || event.ctrlKey;
    const key = event.key;
    const editing = isEditableTarget(event.target);
    const inShell = event.target === document.body || this.root.contains(event.target)
      || Boolean(event.target.closest?.(".cv-shell"));
    if (!inShell) return;
    // Panels beside the canvas (palette, inspector) keep their own keys.
    const inOtherPanel = !this.root.contains(event.target) && event.target !== document.body
      && !event.target.closest?.(".cv-topbar");

    // Save and run work everywhere in the canvas, even mid-sentence.
    if (mod && !event.altKey && key.toLowerCase() === "s") {
      event.preventDefault();
      this.env.actions.saveNow?.();
      return;
    }
    if (mod && key === "Enter") {
      event.preventDefault();
      this.env.actions.runAll?.();
      return;
    }
    if (editing || inOtherPanel) {
      if (key === "Escape" && editing && this.root.contains(event.target)) {
        event.target.blur();
        this.canvas.focus({ preventScroll: true });
      }
      return;
    }
    if (key === " " && !event.repeat) {
      if (event.target instanceof Element && event.target.closest("button, a, [role='menuitem']")) return;
      event.preventDefault();
      this.setSpace(true);
      return;
    }
    if (key === "Escape") {
      if (this.mode) this.cancelMode();
      else if (this.menu.open) this.menu.close();
      else this.clearSelection();
      return;
    }
    if (key === "Delete" || key === "Backspace") {
      if (!this.selection.size && !this.selectedEdge) return;
      event.preventDefault();
      this.deleteSelection();
      return;
    }
    if (mod && key.toLowerCase() === "z") {
      event.preventDefault();
      if (event.shiftKey) this.store.redo();
      else this.store.undo();
      return;
    }
    if (mod && key.toLowerCase() === "y") {
      event.preventDefault();
      this.store.redo();
      return;
    }
    if (mod && key.toLowerCase() === "d") {
      event.preventDefault();
      this.duplicate();
      return;
    }
    if (mod && key.toLowerCase() === "a") {
      event.preventDefault();
      this.selectAll();
      return;
    }
    if (mod) return;
    if (key === "f" || key === "F") {
      event.preventDefault();
      this.fitView(this.selection.size && event.shiftKey ? { nodeIds: [...this.selection] } : {});
      return;
    }
    if (key === "+" || key === "=") { event.preventDefault(); this.zoomBy(1.25); return; }
    if (key === "-" || key === "_") { event.preventDefault(); this.zoomBy(0.8); return; }
    if (key.startsWith("Arrow") && this.selection.size) {
      event.preventDefault();
      const step = event.shiftKey ? 48 : 8;
      const dx = key === "ArrowLeft" ? -step : key === "ArrowRight" ? step : 0;
      const dy = key === "ArrowUp" ? -step : key === "ArrowDown" ? step : 0;
      this.nudge(dx, dy);
    }
  }

  onKeyUp(event) {
    if (event.key === " ") this.setSpace(false);
  }

  setSpace(down) {
    if (this.spaceDown === down) return;
    this.spaceDown = down;
    this.canvas.classList.toggle("is-space", down);
  }

  /* ================================================================ clipboard */

  clipboardAllowed(event) {
    if (!this.isActive() || this.modalOpen() || isEditableTarget(event.target)) return false;
    const target = event.target;
    if (target instanceof Element && !this.root.contains(target) && target !== document.body
      && !target.closest?.(".cv-topbar")) return false;
    const selection = window.getSelection?.();
    if (selection && !selection.isCollapsed && selection.toString()) return false;
    return true;
  }

  onCopy(event, cut) {
    if (!this.clipboardAllowed(event) || !this.selection.size) return;
    const payload = this.store.copy([...this.selection]);
    if (!payload) return;
    event.preventDefault();
    this.env.setClipboard?.(payload);
    event.clipboardData?.setData("text/plain", JSON.stringify(payload));
    if (cut && !this.env.readOnly) this.store.removeNodes([...this.selection]);
  }

  onPaste(event) {
    if (!this.clipboardAllowed(event) || this.env.readOnly) return;
    const data = event.clipboardData;
    const files = [...(data?.files || [])].filter((file) => file.type.startsWith("image/"));
    const at = this.pointer.inside ? this.clientToWorld(this.pointer.clientX, this.pointer.clientY) : null;
    if (files.length) {
      event.preventDefault();
      this.env.actions.addImageFiles?.(files, at || this.viewCenterWorld());
      return;
    }
    const payload = parseClipboardText(data?.getData("text/plain") || "") || this.env.clipboard?.();
    if (!payload) return;
    event.preventDefault();
    this.pasteClipboard({ at, payload });
  }

  /* ================================================================ files */

  carriesFiles(event) {
    return Array.from(event.dataTransfer?.types || []).includes("Files");
  }

  onFileDrag(event) {
    if (!this.carriesFiles(event)) return;
    // Handled here, and kept from app.js's window guard, which would set the
    // drop effect back to "none".
    event.preventDefault();
    event.stopPropagation();
    if (this.env.readOnly) {
      event.dataTransfer.dropEffect = "none";
      return;
    }
    event.dataTransfer.dropEffect = "copy";
    this.dropHint.hidden = false;
    const card = event.target.closest?.(".cv-node");
    const imageCard = card?.dataset.nodeType === "image_input" ? card : null;
    if (this.dropCard !== imageCard) {
      this.dropCard?.classList.remove("is-file-target");
      imageCard?.classList.add("is-file-target");
      this.dropCard = imageCard;
    }
  }

  onFileDragLeave(event) {
    if (!this.carriesFiles(event)) return;
    if (event.relatedTarget && this.canvas.contains(event.relatedTarget)) return;
    this.dropHint.hidden = true;
    this.dropCard?.classList.remove("is-file-target");
    this.dropCard = null;
  }

  onFileDrop(event) {
    if (!this.carriesFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    this.dropHint.hidden = true;
    const card = this.dropCard;
    this.dropCard?.classList.remove("is-file-target");
    this.dropCard = null;
    if (this.env.readOnly) return;
    const files = [...(event.dataTransfer?.files || [])];
    if (!files.length) return;
    this.refreshRect();
    if (card) {
      this.select([card.dataset.cvNode]);
      this.env.actions.uploadImage?.(card.dataset.cvNode, files[0]);
      return;
    }
    this.env.actions.addImageFiles?.(files, this.clientToWorld(event.clientX, event.clientY));
  }

  /* ================================================================ wheel */

  onWheel(event) {
    if (!this.store) return;
    this.refreshRect();
    const zooming = event.ctrlKey || event.metaKey;
    if (!zooming) {
      const scroller = event.target.closest?.("[data-cv-scroll], textarea, .cv-menu, select");
      if (scroller && this.canScroll(scroller, event)) return;
    }
    event.preventDefault();
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? this.rect.height : 1;
    let dx = event.deltaX * unit;
    let dy = event.deltaY * unit;
    if (zooming) {
      // A trackpad pinch arrives as many small deltas; a mouse wheel with
      // Ctrl/⌘ as a few large notches. Pinch follows the fingers, a notch is one step.
      const factor = Math.abs(dy) < 30 ? Math.exp(-dy * 0.012) : (dy < 0 ? 1.15 : 1 / 1.15);
      this.zoomAt(event.clientX, event.clientY, this.viewport.zoom * factor);
      return;
    }
    if (event.shiftKey && !dx) {
      dx = dy;
      dy = 0;
    }
    this.store.setViewport({ x: this.viewport.x - dx, y: this.viewport.y - dy });
  }

  canScroll(element, event) {
    const dy = event.deltaY;
    if (element.scrollHeight <= element.clientHeight + 1) return false;
    if (dy < 0) return element.scrollTop > 0;
    if (dy > 0) return element.scrollTop + element.clientHeight < element.scrollHeight - 1;
    return false;
  }

  onGesture(event, phase) {
    if (!this.store) return;
    event.preventDefault();
    if (phase === "start") {
      this.gestureZoom = this.viewport.zoom;
      return;
    }
    this.refreshRect();
    this.zoomAt(event.clientX, event.clientY, (this.gestureZoom || 1) * event.scale);
  }
}

/* ================================================================ minimap */

class Minimap {
  constructor(editor, host) {
    this.editor = editor;
    this.host = host;
    this.canvas = host.querySelector("canvas");
    this.context = this.canvas.getContext("2d");
    this.map = null;
    this.colors = null;
    this.canvas.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      event.stopPropagation();
      this.dragging = event.pointerId;
      try { this.canvas.setPointerCapture(event.pointerId); } catch (_error) { /* synthetic */ }
      this.navigate(event);
    });
    this.canvas.addEventListener("pointermove", (event) => {
      if (this.dragging === event.pointerId) this.navigate(event);
    });
    const stop = (event) => {
      if (this.dragging === event.pointerId) this.dragging = null;
    };
    this.canvas.addEventListener("pointerup", stop);
    this.canvas.addEventListener("pointercancel", stop);
    this.canvas.addEventListener("wheel", (event) => event.preventDefault(), { passive: false });
  }

  readColors() {
    const style = getComputedStyle(this.host);
    const token = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
    this.colors = {
      node: token("--cv-minimap-node", "#C9CBD3"),
      selected: token("--brand", "#F2A93B"),
      running: token("--brand-mark", "#9E6300"),
      failed: token("--danger", "#C0223E"),
      done: token("--ok", "#147A4E"),
      view: token("--fg-label", "#545A66"),
    };
  }

  draw() {
    const editor = this.editor;
    const store = editor.store;
    const width = this.host.clientWidth;
    const height = this.host.clientHeight;
    if (!width || !height) return;
    const ratio = window.devicePixelRatio || 1;
    if (this.canvas.width !== Math.round(width * ratio) || this.canvas.height !== Math.round(height * ratio)) {
      this.canvas.width = Math.round(width * ratio);
      this.canvas.height = Math.round(height * ratio);
    }
    const ctx = this.context;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    this.host.classList.toggle("is-empty", !store || !store.nodes.length);
    if (!store || !store.nodes.length) {
      this.map = null;
      return;
    }
    if (!this.colors) this.readColors();
    const rect = editor.rect;
    const { x, y, zoom } = store.viewport;
    const view = { x: -x / zoom, y: -y / zoom, w: rect.width / zoom, h: rect.height / zoom };
    const box = editor.bounds(store.nodes);
    const minX = Math.min(box.x, view.x);
    const minY = Math.min(box.y, view.y);
    const maxX = Math.max(box.x + box.w, view.x + view.w);
    const maxY = Math.max(box.y + box.h, view.y + view.h);
    const pad = 8;
    const scale = Math.min((width - pad * 2) / Math.max(1, maxX - minX), (height - pad * 2) / Math.max(1, maxY - minY));
    const offsetX = pad + ((width - pad * 2) - (maxX - minX) * scale) / 2;
    const offsetY = pad + ((height - pad * 2) - (maxY - minY) * scale) / 2;
    this.map = { minX, minY, scale, offsetX, offsetY };
    const results = editor.env.results;
    for (const node of store.nodes) {
      const nodeBox = editor.nodeBox(node);
      const status = results.get(node.id)?.status;
      ctx.fillStyle = editor.selection.has(node.id)
        ? this.colors.selected
        : status === "RUNNING" ? this.colors.running
          : status === "FAILED" ? this.colors.failed
            : this.colors.node;
      ctx.fillRect(
        offsetX + (nodeBox.x - minX) * scale,
        offsetY + (nodeBox.y - minY) * scale,
        Math.max(2, nodeBox.w * scale),
        Math.max(2, nodeBox.h * scale),
      );
    }
    ctx.strokeStyle = this.colors.view;
    ctx.lineWidth = 1.25;
    ctx.fillStyle = "rgba(20, 22, 30, 0.05)";
    const vx = offsetX + (view.x - minX) * scale;
    const vy = offsetY + (view.y - minY) * scale;
    ctx.fillRect(vx, vy, view.w * scale, view.h * scale);
    ctx.strokeRect(vx + 0.5, vy + 0.5, Math.max(1, view.w * scale - 1), Math.max(1, view.h * scale - 1));
  }

  navigate(event) {
    if (!this.map || !this.editor.store) return;
    const bounds = this.canvas.getBoundingClientRect();
    const { minX, minY, scale, offsetX, offsetY } = this.map;
    const worldX = (event.clientX - bounds.left - offsetX) / scale + minX;
    const worldY = (event.clientY - bounds.top - offsetY) / scale + minY;
    this.editor.centerOnWorld(worldX, worldY);
  }
}
