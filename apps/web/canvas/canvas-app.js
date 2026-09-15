/**
 * Best Shiny — the canvas shell (`/app`, `/app/canvas/<workflow id>`).
 *
 * Owns the top bar, the palette, the editor, the inspector, autosave and run
 * polling for one open canvas. It never owns the session: app.js decides who
 * is signed in and says so with `ai-director:auth`; the canvas asks app.js to
 * sign out (`ai-director:request-logout`) or to lock an expired session
 * (`ai-director:session-expired`). Every sign-out starts a new epoch, and a
 * response that belongs to an older epoch is dropped where it lands.
 */
import { currentRoute, isCanvasRoute, navigate, onRoute } from "../router.js";
import { api, imageFileProblem, nameClipboardImage, onUnauthorized, uploadImage } from "./api.js";
import { ConnectionsDialog } from "./connections.js";
import { CanvasEditor } from "./editor.js";
import { GraphStore, indexCatalog, runScope, starterGraph, validateForRun } from "./graph.js";
import { icon } from "./icons.js";
import { Inspector } from "./inspector.js";
import { MediaCache } from "./media.js";
import { nodeTitle } from "./node-card.js";
import { Palette } from "./palette.js";
import {
  MOD, PopupMenu, confirmSheet, copyText, escapeHTML, hideToast, promptSheet, relativeTime, toast,
} from "./ui.js";

const SAVE_DEBOUNCE = 800;
const VIEWPORT_DEBOUNCE = 3000;
const POLL_FAST = 1500;
const POLL_SLOW = 5000;
const POLL_SLOW_AFTER = 120_000;
const ACTIVE_RUN = new Set(["QUEUED", "RUNNING"]);
const LAST_KEY = "bestshiny:canvas:last";
const PANELS_KEY = "bestshiny:canvas:panels";
const ROLE_RANK = { VIEWER: 10, EDITOR: 20, ADMIN: 30, OWNER: 40 };
const STARTER_NAME = "My first canvas";

const shell = document.getElementById("canvasShell");

/* ================================================================== state */

const app = {
  booted: false,
  active: false,
  epoch: 0,
  user: null,
  sessionExpired: false,
  catalog: null,
  projects: [],
  projectId: null,
  project: null,
  workspaceId: null,
  announcedWorkspace: null,
  workflows: [],
  workflowId: null,
  workflow: null,
  store: null,
  projectSeq: 0,
  workflowSeq: 0,
  billing: null,
  selection: { nodeIds: [], edgeId: null },
  clipboard: null,
  save: null,
  run: { active: null, poll: null, starting: false, stopping: false },
  freshWorkflowId: null,
  stage: "idle",
  panels: readPanels(),
};

const media = new MediaCache();

const env = {
  store: null,
  catalog: null,
  readOnly: true,
  runActive: false,
  results: new Map(),
  lastOutputs: new Map(),
  errors: new Map(),
  uploads: new Map(),
  connections: [],
  canManageConnections: false,
  videoModels: [],
  media,
  workflow: null,
  runs: null,
  isActive: () => app.active,
  clipboard: () => app.clipboard,
  setClipboard: (payload) => { app.clipboard = payload; },
  onSelectionChange: (selection) => {
    app.selection = selection;
    inspector?.setSelection(selection);
    renderRunControls();
  },
  actions: {
    toast: (message) => toast(message),
    copyText: async (text) => toast((await copyText(text)) ? "Copied to the clipboard" : "Copy did not work in this browser"),
    runNodes: (ids) => startRun({ nodeIds: ids }),
    runAll: () => startRun({}),
    saveNow: () => saveNow(),
    topUp: () => openWallet(),
    openConnections: ({ capability = null } = {}) => openConnections({ capability }),
    uploadImage: (nodeId, file) => uploadNodeImage(nodeId, file),
    addImageFiles: (files, at) => addImageFiles(files, at),
    openViewer: (options) => openViewer(options),
    renameWorkflow: (name) => renameWorkflow(name),
    duplicate: (ids) => editor?.duplicate(ids),
    revealNode: (id) => editor?.revealNode(id),
  },
};

let editor = null;
let inspector = null;
let palette = null;
let connectionsDialog = null;
let menu = null;
let viewer = null;

const staleEpoch = (epoch) => epoch !== app.epoch;
const $ = (name) => shell?.querySelector(`[data-cv-el="${name}"]`);

/* ================================================================== small helpers */

function readPanels() {
  try {
    const saved = JSON.parse(localStorage.getItem(PANELS_KEY) || "{}");
    return { palette: saved.palette !== false, inspector: saved.inspector !== false };
  } catch (_error) {
    return { palette: true, inspector: true };
  }
}

function writePanels() {
  try { localStorage.setItem(PANELS_KEY, JSON.stringify(app.panels)); } catch (_error) { /* storage blocked */ }
}

function userKey(user = app.user) {
  return user?.id || user?.email || "";
}

function readLast() {
  try {
    const saved = JSON.parse(localStorage.getItem(LAST_KEY) || "{}");
    return saved?.[userKey()] || null;
  } catch (_error) {
    return null;
  }
}

function rememberLast() {
  if (!app.user || !app.projectId) return;
  try {
    const saved = JSON.parse(localStorage.getItem(LAST_KEY) || "{}");
    saved[userKey()] = { projectId: app.projectId, workflowId: app.workflowId };
    localStorage.setItem(LAST_KEY, JSON.stringify(saved));
  } catch (_error) { /* storage blocked */ }
}

function workflowIdFromRoute(route) {
  const match = /^\/app\/canvas\/([A-Za-z0-9_-]{1,64})$/.exec(route || "");
  return match ? match[1] : null;
}

function roleFor(workspaceId) {
  const workspace = (app.user?.workspaces || []).find((item) => item.id === workspaceId);
  return workspace?.role || null;
}

/** Editing needs EDITOR. A development identity with no listed membership is left to the server. */
function canEdit() {
  const role = roleFor(app.workspaceId);
  if (!role) return true;
  return (ROLE_RANK[role] || 0) >= ROLE_RANK.EDITOR;
}

function summaryOf(workflow) {
  return {
    id: workflow.id,
    project_id: workflow.project_id,
    name: workflow.name,
    version: workflow.version,
    node_count: (workflow.graph?.nodes || []).length,
    created_at: workflow.created_at,
    updated_at: workflow.updated_at,
    last_run: null,
  };
}

/* ================================================================== boot */

function boot() {
  renderShell();
  menu = new PopupMenu(shell);
  editor = new CanvasEditor($("stage"), env);
  inspector = new Inspector($("inspector-body"), env);
  const afterPaletteAdd = () => {
    if (narrow() && panelOpen("palette")) setPanel("palette", false);
  };
  palette = new Palette($("palette"), {
    onAdd: (type) => {
      if (editor.store && editor.addNodeAtCenter(type)) afterPaletteAdd();
    },
    onDropAt: (type, clientX, clientY) => {
      if (editor.store && editor.addNodeAtClient(type, clientX, clientY)) afterPaletteAdd();
    },
    isInsideCanvas: (clientX, clientY) => app.stage === "ready" && editor.isClientInside(clientX, clientY),
    onCollapse: () => setPanel("palette", false),
  });
  connectionsDialog = new ConnectionsDialog({
    onChanged: (workspaceId, connections) => {
      if (workspaceId !== app.workspaceId) return;
      env.connections = connections;
      refreshConnectionContext();
    },
    toast,
  });
  bindShell();
  applyPanels();
  setStage("idle");
  onUnauthorized(handleUnauthorized);
  onRoute(handleRoute);
  window.addEventListener("ai-director:auth", (event) => {
    const user = event.detail || null;
    if (!user) {
      resetSession();
      return;
    }
    app.sessionExpired = false;
    if (app.user && userKey(app.user) !== userKey(user)) resetSession();
    app.user = user;
    renderUser();
  });
  window.addEventListener("ai-director:plan-changed", () => {
    if (!app.user) return;
    loadBilling();
    loadVideoModels();
  });
  document.getElementById("walletDialog")?.addEventListener("close", () => {
    if (app.active) loadBilling();
  });
  window.addEventListener("beforeunload", (event) => {
    const save = app.save;
    if (!app.active || !save || env.readOnly) return;
    if (save.graphDirty || save.pendingName !== null || save.inFlight) {
      runSave({ keepalive: true });
      event.preventDefault();
      event.returnValue = "";
    }
  });
  window.addEventListener("pagehide", () => {
    if (app.save && (app.save.graphDirty || app.save.viewportDirty)) runSave({ keepalive: true });
  });
  matchMedia("(max-width: 1099px)").addEventListener("change", applyPanels);
}

function renderShell() {
  shell.innerHTML = `
    <header class="cv-topbar">
      <a class="app-brand" href="/app" data-link title="Best Shiny"><span class="brand-mark" aria-hidden="true">B</span><span class="sr-only">Best Shiny canvas</span></a>
      <div class="project-switcher cv-project">
        <select data-cv-el="project" aria-label="Current project"><option value="">Loading…</option></select>
        <button type="button" class="project-switcher-add" data-cv-el="new-project" title="New project" aria-label="New project">+</button>
      </div>
      <span class="cv-topbar-sep" aria-hidden="true"></span>
      <div class="cv-wf">
        <button type="button" class="cv-wf-switch" data-cv-el="wf-switch" aria-haspopup="menu" aria-expanded="false" title="Switch canvas" aria-label="Switch canvas">${icon("grid", { size: 15 })}${icon("chevron", { size: 12 })}</button>
        <input type="text" class="cv-wf-name" data-cv-el="wf-name" maxlength="200" aria-label="Canvas name" spellcheck="false" autocomplete="off" disabled />
        <span class="cv-save" data-cv-el="save" role="status" aria-live="polite"></span>
      </div>
      <div class="cv-topbar-right">
        <button type="button" class="btn btn-tertiary cv-panel-toggle" data-cv-el="toggle-palette" aria-pressed="true" title="Show or hide the nodes panel">${icon("panel", { size: 15 })}<span>Nodes</span></button>
        <button type="button" class="btn btn-tertiary cv-panel-toggle" data-cv-el="toggle-inspector" aria-pressed="true" title="Show or hide the details panel">${icon("panel-right", { size: 15 })}<span>Details</span></button>
        <button type="button" class="btn btn-secondary cv-conn-btn" data-cv-el="connections" title="Your own model API keys">${icon("plug", { size: 15 })}<span>API connections</span></button>
        <div class="cv-run-group">
          <button type="button" class="btn btn-primary cv-run" data-cv-el="run" title="Run everything (${MOD}+Enter)">${icon("play", { size: 13 })}<span data-cv-el="run-label">Run</span></button>
          <button type="button" class="btn btn-primary cv-run-more" data-cv-el="run-menu" aria-haspopup="menu" aria-expanded="false" aria-label="More ways to run" title="More ways to run">${icon("chevron", { size: 12 })}</button>
        </div>
        <button type="button" class="btn btn-danger cv-stop" data-cv-el="stop" hidden>${icon("stop", { size: 12 })}<span>Stop</span></button>
        <button type="button" class="credits-pill cv-credits" data-cv-el="credits" title="Credits and top up"><span class="credits-dot" aria-hidden="true"></span><span class="mono" data-cv-el="credits-amount">—</span></button>
        <div class="user-menu cv-user">
          <button type="button" class="user-menu-btn" data-cv-el="user-btn" aria-haspopup="true" aria-expanded="false">
            <span class="user-avatar" data-cv-el="avatar" aria-hidden="true">·</span>
            <span class="user-menu-name" data-cv-el="user-name"></span>
            <span class="user-menu-caret" aria-hidden="true">▾</span>
          </button>
          <div class="user-menu-panel" data-cv-el="user-menu" hidden>
            <span class="user-menu-email" data-cv-el="user-email"></span>
            <button type="button" class="user-menu-action" data-cv-el="go-studio">Studio (classic workbench)</button>
            <button type="button" class="user-menu-action" data-cv-el="go-admin" hidden>Admin · System health</button>
            <div class="user-menu-sep"></div>
            <button type="button" class="user-menu-action is-danger" data-cv-el="sign-out">Sign out</button>
          </div>
        </div>
      </div>
    </header>
    <div class="cv-banner" data-cv-el="banner" hidden></div>
    <div class="cv-main" data-cv-el="main">
      <aside class="cv-palette" data-cv-el="palette" aria-label="Nodes"></aside>
      <main class="cv-stage" data-cv-el="stage">
        <div class="cv-stage-state" data-cv-el="stage-state" hidden></div>
      </main>
      <aside class="cv-inspector" data-cv-el="inspector" aria-label="Details">
        <div class="cv-panel-head">
          <h2 class="cv-panel-title">Details</h2>
          <button type="button" class="cv-icon-btn" data-cv-el="close-inspector" aria-label="Hide the details panel" title="Hide panel">${icon("chevron-right", { size: 15 })}</button>
        </div>
        <div class="cv-inspector-body" data-cv-el="inspector-body"></div>
      </aside>
    </div>`;
}

function bindShell() {
  $("project").addEventListener("change", async (event) => {
    const projectId = event.target.value;
    if (!projectId || projectId === app.projectId) return;
    if (!(await confirmLeave())) {
      event.target.value = app.projectId || "";
      return;
    }
    selectProject(projectId).catch(reportError);
  });
  $("new-project").addEventListener("click", () => createProjectFlow());
  $("wf-switch").addEventListener("click", (event) => openWorkflowMenu(event.currentTarget));
  const nameInput = $("wf-name");
  nameInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); nameInput.blur(); }
    if (event.key === "Escape") { nameInput.value = app.workflow?.name || ""; nameInput.blur(); }
  });
  nameInput.addEventListener("change", () => renameWorkflow(nameInput.value));
  $("save").addEventListener("click", (event) => {
    if (event.target.closest("[data-cv-save-retry]")) saveNow();
  });
  $("toggle-palette").addEventListener("click", () => setPanel("palette", !panelOpen("palette")));
  $("toggle-inspector").addEventListener("click", () => setPanel("inspector", !panelOpen("inspector")));
  $("close-inspector").addEventListener("click", () => setPanel("inspector", false));
  $("connections").addEventListener("click", () => openConnections({}));
  $("run").addEventListener("click", () => startRun({}));
  $("run-menu").addEventListener("click", (event) => openRunMenu(event.currentTarget));
  $("stop").addEventListener("click", () => stopRun());
  $("credits").addEventListener("click", () => openWallet());
  $("user-btn").addEventListener("click", (event) => {
    const panel = $("user-menu");
    const open = panel.hidden;
    panel.hidden = !open;
    event.currentTarget.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest?.(".cv-user")) {
      $("user-menu").hidden = true;
      $("user-btn").setAttribute("aria-expanded", "false");
    }
  });
  $("go-studio").addEventListener("click", async () => {
    $("user-menu").hidden = true;
    if (await confirmLeave()) navigate("/app/studio");
  });
  $("go-admin").addEventListener("click", async () => {
    $("user-menu").hidden = true;
    if (await confirmLeave()) navigate("/admin");
  });
  $("sign-out").addEventListener("click", async () => {
    $("user-menu").hidden = true;
    if (await confirmLeave()) window.dispatchEvent(new CustomEvent("ai-director:request-logout"));
  });
  $("stage").addEventListener("pointerdown", () => {
    if (!narrow()) return;
    if (panelOpen("palette")) setPanel("palette", false);
    if (panelOpen("inspector")) setPanel("inspector", false);
  }, true);
  $("banner").addEventListener("click", (event) => {
    const action = event.target.closest("[data-cv-banner]")?.dataset.cvBanner;
    if (action) bannerActions[action]?.();
  });
  $("stage-state").addEventListener("click", (event) => {
    const action = event.target.closest("[data-cv-stage]")?.dataset.cvStage;
    if (action) stageActions[action]?.();
  });
}

/* ================================================================== panels */

const narrow = () => matchMedia("(max-width: 1099px)").matches;

function panelOpen(name) {
  const main = $("main");
  return narrow() ? main.classList.contains(`show-${name}`) : app.panels[name];
}

function setPanel(name, open) {
  if (narrow()) {
    $("main").classList.toggle(`show-${name}`, open);
    if (open) $("main").classList.remove(`show-${name === "palette" ? "inspector" : "palette"}`);
  } else {
    app.panels[name] = open;
    writePanels();
  }
  applyPanels();
  requestAnimationFrame(() => editor?.refreshRect());
}

function applyPanels() {
  const main = $("main");
  if (!main) return;
  const isNarrow = narrow();
  main.classList.toggle("is-narrow", isNarrow);
  main.classList.toggle("hide-palette", !isNarrow && !app.panels.palette);
  main.classList.toggle("hide-inspector", !isNarrow && !app.panels.inspector);
  if (!isNarrow) main.classList.remove("show-palette", "show-inspector");
  $("toggle-palette").setAttribute("aria-pressed", String(panelOpen("palette")));
  $("toggle-inspector").setAttribute("aria-pressed", String(panelOpen("inspector")));
}

/* ================================================================== route and session */

function handleRoute(route, user) {
  if (!user) {
    resetSession();
    return;
  }
  if (!isCanvasRoute(route)) {
    deactivate();
    return;
  }
  if (app.user && userKey(app.user) !== userKey(user)) resetSession();
  app.user = user;
  app.sessionExpired = false;
  renderUser();
  const wanted = workflowIdFromRoute(route);
  const wasActive = app.active;
  app.active = true;
  if (!app.booted) {
    app.booted = true;
    bootSession(wanted);
    return;
  }
  if (wanted && wanted !== app.workflowId && !app.opening) {
    openWorkflowById(wanted).catch(reportError);
    return;
  }
  if (!wasActive) resume();
}

function deactivate() {
  if (!app.active) return;
  app.active = false;
  flushSave().catch(() => null);
  stopPolling();
  menu?.close({ silent: true });
  connectionsDialog?.close();
  viewer?.close();
  hideToast();
}

/** Coming back to the canvas from the studio: catch up on what ran meanwhile. */
async function resume() {
  announceWorkspace(true);
  loadBilling();
  syncUrl();
  if (!app.workflowId || !app.store) {
    if (app.stage === "error" || app.stage === "idle") {
      app.booted = true;
      bootSession(null);
    }
    return;
  }
  const epoch = app.epoch;
  const workflowId = app.workflowId;
  try {
    const workflow = await api.workflow(workflowId);
    if (staleEpoch(epoch) || app.workflowId !== workflowId) return;
    applyNodeResults(workflow.node_results || {});
    if (workflow.active_run) applyRun(workflow.active_run);
    else if (app.run.active && ACTIVE_RUN.has(app.run.active.status)) {
      app.run.active = null;
      env.runActive = false;
      editor.cards.refresh();
      renderRunControls();
    }
    loadRuns();
  } catch (_error) { /* the next action reports it */ }
}

function resetSession() {
  app.epoch += 1;
  stopPolling();
  if (app.save) {
    clearTimeout(app.save.timer);
    clearTimeout(app.save.retryTimer);
  }
  menu?.close({ silent: true });
  connectionsDialog?.close();
  viewer?.close();
  hideToast();
  editor?.detach();
  media.clear();
  Object.assign(app, {
    booted: false,
    active: false,
    user: null,
    projects: [],
    projectId: null,
    project: null,
    workspaceId: null,
    announcedWorkspace: null,
    workflows: [],
    workflowId: null,
    workflow: null,
    store: null,
    billing: null,
    selection: { nodeIds: [], edgeId: null },
    clipboard: null,
    save: null,
    run: { active: null, poll: null, starting: false, stopping: false },
    freshWorkflowId: null,
    opening: false,
  });
  Object.assign(env, {
    store: null,
    readOnly: true,
    runActive: false,
    results: new Map(),
    lastOutputs: new Map(),
    errors: new Map(),
    uploads: new Map(),
    connections: [],
    canManageConnections: false,
    videoModels: [],
    workflow: null,
    runs: null,
  });
  if (!shell) return;
  $("project").innerHTML = '<option value="">—</option>';
  $("wf-name").value = "";
  $("wf-name").disabled = true;
  $("save").innerHTML = "";
  $("credits-amount").textContent = "—";
  $("user-name").textContent = "";
  $("user-email").textContent = "";
  $("avatar").textContent = "·";
  $("user-menu").hidden = true;
  hideBanner();
  inspector?.setSelection({ nodeIds: [], edgeId: null });
  palette?.setReadOnly(true);
  renderRunControls();
  setStage("idle");
}

function handleUnauthorized() {
  if (app.sessionExpired) return;
  app.sessionExpired = true;
  stopPolling();
  // app.js owns the session: it locks the page exactly as its own 401 path
  // does, which resets this shell too - so the notice comes after the lock.
  window.dispatchEvent(new CustomEvent("ai-director:session-expired"));
  navigate("/login", { replace: true });
  toast("Your session has expired. Sign in again to keep working.", { tone: "danger" });
}

function renderUser() {
  const user = app.user;
  if (!user || !shell) return;
  const name = user.display_name || user.email || "";
  $("user-name").textContent = name;
  $("user-email").textContent = user.email || "";
  $("avatar").textContent = (name.trim()[0] || "·").toUpperCase();
  $("go-admin").hidden = !["ADMIN", "SUPER_ADMIN"].includes(user.platform_role);
}

function reportError(error) {
  if (!error || error.name === "AbortError" || error.status === 401) return;
  toast(error.message || "Something went wrong.", { tone: "danger" });
}

/* ================================================================== stage states */

const stageActions = {
  retry: () => {
    app.booted = true;
    bootSession(workflowIdFromRoute(currentRoute()));
  },
  "new-project": () => createProjectFlow(),
  "new-canvas": () => createWorkflowFlow(),
};

function setStage(kind, message = "") {
  app.stage = kind;
  const host = $("stage-state");
  if (!host) return;
  const stage = $("stage");
  stage.classList.toggle("is-covered", kind !== "ready");
  if (kind === "ready") {
    host.hidden = true;
    host.innerHTML = "";
    return;
  }
  host.hidden = false;
  if (kind === "idle") {
    host.innerHTML = "";
  } else if (kind === "loading") {
    host.innerHTML = `<div class="cv-stage-card" role="status"><span class="cv-spinner is-large" aria-hidden="true"></span><p>${escapeHTML(message || "Loading…")}</p></div>`;
  } else if (kind === "error") {
    host.innerHTML = `<div class="empty-block cv-stage-card" role="alert">
        <span class="empty-icon">${icon("alert", { size: 22 })}</span>
        <strong>The canvas could not open</strong>
        <p>${escapeHTML(message || "Something went wrong.")}</p>
        <div class="btn-row btn-row-center"><button type="button" class="btn btn-primary" data-cv-stage="retry">Try again</button></div>
      </div>`;
  } else if (kind === "no-projects") {
    host.innerHTML = `<div class="empty-block cv-stage-card">
        <span class="empty-icon">${icon("grid", { size: 22 })}</span>
        <strong>Create your first project</strong>
        <p>A project holds your canvases, uploads and generations. Start one and we will set up a canvas you can run right away.</p>
        <div class="btn-row btn-row-center"><button type="button" class="btn btn-primary" data-cv-stage="new-project">Create your first project</button></div>
      </div>`;
  } else if (kind === "no-canvas") {
    host.innerHTML = `<div class="empty-block cv-stage-card">
        <span class="empty-icon">${icon("grid", { size: 22 })}</span>
        <strong>No canvases in this project</strong>
        <p>${env.readOnly ? "Ask a workspace editor to create one. You can view canvases once they exist." : "Create a canvas to start connecting prompts, images and models."}</p>
        ${env.readOnly ? "" : '<div class="btn-row btn-row-center"><button type="button" class="btn btn-primary" data-cv-stage="new-canvas">New canvas</button></div>'}
      </div>`;
  }
}

/* ================================================================== banner */

const bannerActions = {};

function showBanner({ kind, tone = "warn", message, actions = [] }) {
  const banner = $("banner");
  banner.hidden = false;
  banner.dataset.kind = kind;
  banner.dataset.tone = tone;
  for (const key of Object.keys(bannerActions)) delete bannerActions[key];
  banner.innerHTML = `${icon(tone === "danger" ? "alert" : "info", { size: 16 })}<span class="cv-banner-text">${escapeHTML(message)}</span>
    <span class="cv-banner-actions">${actions.map((action, index) => {
      bannerActions[`a${index}`] = action.onClick;
      return `<button type="button" class="btn ${action.primary ? "btn-primary" : "btn-tertiary"} cv-btn-sm" data-cv-banner="a${index}">${escapeHTML(action.label)}</button>`;
    }).join("")}</span>`;
  requestAnimationFrame(() => editor?.refreshRect());
}

function hideBanner(kind = null) {
  const banner = $("banner");
  if (!banner || banner.hidden || (kind && banner.dataset.kind !== kind)) return;
  banner.hidden = true;
  banner.innerHTML = "";
  requestAnimationFrame(() => editor?.refreshRect());
}

/* ================================================================== loading */

async function loadCatalog() {
  if (app.catalog) return app.catalog;
  const raw = await api.nodeTypes();
  app.catalog = indexCatalog(raw);
  env.catalog = app.catalog;
  palette.setCatalog(app.catalog);
  return app.catalog;
}

async function bootSession(wantedWorkflowId) {
  const epoch = app.epoch;
  setStage("loading", "Opening your canvas…");
  try {
    const [, projects] = await Promise.all([loadCatalog(), api.listProjects()]);
    if (staleEpoch(epoch)) return;
    app.projects = Array.isArray(projects) ? projects : [];
    renderProjectSelect();
    if (wantedWorkflowId) {
      const opened = await openWorkflowById(wantedWorkflowId, { quiet: true });
      if (opened || staleEpoch(epoch)) return;
    }
    if (!app.projects.length) {
      setStage("no-projects");
      renderTopbar();
      return;
    }
    const last = readLast();
    const projectId = app.projects.some((project) => project.id === last?.projectId) ? last.projectId : app.projects[0].id;
    await selectProject(projectId, { preferWorkflowId: last?.projectId === projectId ? last.workflowId : null, epoch });
  } catch (error) {
    if (staleEpoch(epoch) || error.status === 401) return;
    setStage("error", error.message);
  }
}

function renderProjectSelect() {
  const select = $("project");
  if (!select) return;
  select.innerHTML = app.projects.length
    ? app.projects.map((project) => `<option value="${escapeHTML(project.id)}">${escapeHTML(project.name || project.title || "Untitled project")}</option>`).join("")
    : '<option value="">No projects yet</option>';
  select.value = app.projectId || "";
  select.disabled = !app.projects.length;
}

async function selectProject(projectId, { preferWorkflowId = null, epoch = app.epoch } = {}) {
  await flushSave();
  if (staleEpoch(epoch)) return;
  const sequence = ++app.projectSeq;
  const stale = () => staleEpoch(epoch) || sequence !== app.projectSeq;
  closeWorkflow();
  app.projectId = projectId;
  renderProjectSelect();
  setStage("loading", "Opening the project…");
  let project;
  try {
    project = await api.getProject(projectId);
  } catch (error) {
    if (stale() || error.status === 401) return;
    setStage("error", error.message);
    return;
  }
  if (stale()) return;
  app.project = project;
  app.workspaceId = project.workspace_id || null;
  env.readOnly = !canEdit();
  palette.setReadOnly(env.readOnly);
  announceWorkspace();
  env.connections = [];
  env.videoModels = [];
  loadBilling();
  loadConnections();
  loadVideoModels();
  let list;
  try {
    list = await api.workflows(projectId);
  } catch (error) {
    if (stale() || error.status === 401) return;
    setStage("error", error.message);
    return;
  }
  if (stale()) return;
  app.workflows = list?.workflows || [];
  let target = app.workflows.find((item) => item.id === preferWorkflowId) || app.workflows[0] || null;
  if (!target) {
    if (env.readOnly) {
      setStage("no-canvas");
      renderTopbar();
      return;
    }
    setStage("loading", "Setting up your first canvas…");
    try {
      const created = await api.createWorkflow(projectId, { name: STARTER_NAME, graph: starterGraph(app.catalog) });
      if (stale()) return;
      app.workflows = [summaryOf(created)];
      app.freshWorkflowId = created.id;
      target = created;
    } catch (error) {
      if (stale() || error.status === 401) return;
      if (error.status === 403) {
        env.readOnly = true;
        palette.setReadOnly(true);
        setStage("no-canvas");
      } else {
        setStage("error", error.message);
      }
      renderTopbar();
      return;
    }
  }
  await openWorkflow(target.id, { epoch, guard: stale });
}

/** A deep link: open a canvas, switching project first when it lives elsewhere. */
async function openWorkflowById(workflowId, { quiet = false } = {}) {
  const epoch = app.epoch;
  app.opening = true;
  try {
    const workflow = await api.workflow(workflowId);
    if (staleEpoch(epoch)) return false;
    if (workflow.project_id !== app.projectId || !app.project) {
      if (!app.projects.some((project) => project.id === workflow.project_id)) {
        const projects = await api.listProjects();
        if (staleEpoch(epoch)) return false;
        app.projects = Array.isArray(projects) ? projects : [];
      }
      await selectProject(workflow.project_id, { preferWorkflowId: workflowId, epoch });
      return app.workflowId === workflowId;
    }
    await flushSave();
    return await openWorkflow(workflowId, { epoch, preloaded: workflow });
  } catch (error) {
    if (staleEpoch(epoch) || error.status === 401) return false;
    if (!quiet || error.status !== 404) toast(error.status === 404 ? "That canvas no longer exists." : error.message);
    else toast("That canvas no longer exists. Opening your latest canvas instead.");
    return false;
  } finally {
    app.opening = false;
  }
}

async function openWorkflow(workflowId, { epoch = app.epoch, preloaded = null, guard = null } = {}) {
  const sequence = ++app.workflowSeq;
  // `guard` is the caller's own fence, e.g. a project switch that has since been superseded.
  const stale = () => staleEpoch(epoch) || sequence !== app.workflowSeq || Boolean(guard?.());
  setStage("loading", "Opening the canvas…");
  let workflow = preloaded;
  if (!workflow) {
    try {
      workflow = await api.workflow(workflowId);
    } catch (error) {
      if (stale() || error.status === 401) return false;
      setStage("error", error.message);
      return false;
    }
  }
  if (stale()) return false;
  closeWorkflow();
  app.workflowId = workflow.id;
  app.workflow = {
    id: workflow.id,
    name: workflow.name,
    version: workflow.version,
    project_id: workflow.project_id,
    workspace_id: workflow.workspace_id,
    updated_at: workflow.updated_at,
  };
  env.workflow = app.workflow;
  app.save = {
    version: workflow.version,
    seq: 0,
    graphDirty: false,
    viewportDirty: false,
    pendingName: null,
    timer: null,
    retryTimer: null,
    inFlight: null,
    again: false,
    conflict: false,
    error: null,
    failures: 0,
    state: "saved",
  };
  env.results = new Map();
  env.lastOutputs = new Map();
  env.errors = new Map();
  env.uploads = new Map();
  applyNodeResults(workflow.node_results || {}, { render: false });
  const store = new GraphStore({ catalog: app.catalog, document: workflow.graph });
  app.store = store;
  editor.attach(store);
  store.subscribe(onDocumentChange);
  inspector.setSelection({ nodeIds: [], edgeId: null });
  if (!app.workflows.some((item) => item.id === workflow.id)) app.workflows.unshift(summaryOf(workflow));
  setStage("ready");
  const viewport = workflow.graph?.viewport;
  const untouched = !viewport || (!viewport.x && !viewport.y && (viewport.zoom === 1 || !viewport.zoom));
  if (app.freshWorkflowId === workflow.id || (untouched && store.nodes.length)) {
    requestAnimationFrame(() => {
      if (app.store === store) editor.fitView({ animate: false, maxZoom: 1 });
    });
  }
  app.freshWorkflowId = null;
  rememberLast();
  renderTopbar();
  syncUrl();
  loadRuns();
  if (workflow.active_run) applyRun(workflow.active_run);
  return true;
}

function closeWorkflow() {
  stopPolling();
  if (app.save) {
    clearTimeout(app.save.timer);
    clearTimeout(app.save.retryTimer);
  }
  menu?.close({ silent: true });
  editor.detach();
  app.store = null;
  app.workflowId = null;
  app.workflow = null;
  app.save = null;
  app.selection = { nodeIds: [], edgeId: null };
  app.run = { active: null, poll: null, starting: false, stopping: false };
  Object.assign(env, {
    workflow: null, runActive: false, runs: null,
    results: new Map(), lastOutputs: new Map(), errors: new Map(), uploads: new Map(),
  });
  hideBanner("conflict");
  hideBanner("deleted");
  renderTopbar();
}

function syncUrl() {
  if (!app.active || !app.workflowId) return;
  const path = `/app/canvas/${app.workflowId}`;
  if (currentRoute() !== path) navigate(path, { replace: true });
}

function announceWorkspace(force = false) {
  if (!app.workspaceId) return;
  if (!force && app.announcedWorkspace === app.workspaceId) return;
  app.announcedWorkspace = app.workspaceId;
  window.dispatchEvent(new CustomEvent("ai-director:workspace-changed", {
    detail: { workspaceId: app.workspaceId, projectId: app.projectId },
  }));
}

async function loadBilling() {
  const workspaceId = app.workspaceId;
  const epoch = app.epoch;
  if (!workspaceId) return;
  try {
    const billing = await api.billing(workspaceId);
    if (staleEpoch(epoch) || app.workspaceId !== workspaceId) return;
    app.billing = billing;
    const balance = Number(billing?.credit_balance);
    $("credits-amount").textContent = Number.isFinite(balance) ? `${balance.toLocaleString()} credits` : "—";
  } catch (_error) {
    if (!staleEpoch(epoch) && app.workspaceId === workspaceId) $("credits-amount").textContent = "—";
  }
}

async function loadConnections() {
  const workspaceId = app.workspaceId;
  const epoch = app.epoch;
  if (!workspaceId) return;
  try {
    const result = await api.connections(workspaceId);
    if (staleEpoch(epoch) || app.workspaceId !== workspaceId) return;
    env.connections = result?.connections || [];
    env.canManageConnections = Boolean(result?.can_manage);
    refreshConnectionContext();
  } catch (_error) {
    if (staleEpoch(epoch) || app.workspaceId !== workspaceId) return;
    env.connections = [];
    refreshConnectionContext();
  }
}

async function loadVideoModels() {
  const projectId = app.projectId;
  const epoch = app.epoch;
  if (!projectId) return;
  try {
    const models = await api.videoModels(projectId);
    if (staleEpoch(epoch) || app.projectId !== projectId) return;
    env.videoModels = Array.isArray(models) ? models : [];
    refreshConnectionContext();
  } catch (_error) { /* the picker keeps Automatic */ }
}

function refreshConnectionContext() {
  if (editor?.store) editor.cards.refreshContext();
  inspector?.refreshContext();
}

async function loadRuns() {
  const workflowId = app.workflowId;
  const epoch = app.epoch;
  if (!workflowId) return;
  env.runs = null;
  inspector.refreshResults([]);
  try {
    const result = await api.runs(workflowId, 20);
    if (staleEpoch(epoch) || app.workflowId !== workflowId) return;
    env.runs = result?.runs || [];
  } catch (_error) {
    if (staleEpoch(epoch) || app.workflowId !== workflowId) return;
    env.runs = [];
  }
  inspector.refreshResults([]);
}

function applyNodeResults(results, { render = true } = {}) {
  const changed = [];
  for (const [nodeId, nodeRun] of Object.entries(results || {})) {
    env.results.set(nodeId, nodeRun);
    if (nodeRun.status === "SUCCEEDED" || nodeRun.status === "CACHED") env.lastOutputs.set(nodeId, nodeRun);
    changed.push(nodeId);
  }
  if (render && editor.store) {
    editor.cards.refresh(changed);
    inspector.refreshResults(changed);
    editor.schedule("minimap");
  }
}

/* ================================================================== top bar */

function renderTopbar() {
  if (!shell) return;
  const nameInput = $("wf-name");
  if (document.activeElement !== nameInput) nameInput.value = app.workflow?.name || "";
  nameInput.disabled = !app.workflow || env.readOnly;
  $("wf-switch").disabled = !app.projectId;
  $("connections").disabled = !app.workspaceId;
  renderSaveState();
  renderRunControls();
}

function setSaveState(state) {
  if (!app.save) return;
  app.save.state = state;
  renderSaveState();
}

function renderSaveState() {
  const host = $("save");
  if (!host) return;
  const save = app.save;
  if (!app.workflow || !save) {
    host.innerHTML = "";
    host.dataset.state = "";
    return;
  }
  if (env.readOnly) {
    host.dataset.state = "readonly";
    host.innerHTML = `${icon("eye", { size: 13 })}<span>View only</span>`;
    return;
  }
  const state = save.state;
  host.dataset.state = state;
  if (state === "saving") host.innerHTML = '<span class="cv-spinner" aria-hidden="true"></span><span>Saving…</span>';
  else if (state === "dirty") host.innerHTML = "<span>Unsaved changes</span>";
  else if (state === "error") host.innerHTML = `${icon("alert", { size: 13 })}<span>Save failed –</span><button type="button" class="cv-link" data-cv-save-retry>retry</button>`;
  else if (state === "conflict") host.innerHTML = `${icon("alert", { size: 13 })}<span>Changed elsewhere</span>`;
  else host.innerHTML = `${icon("check", { size: 13 })}<span>Saved</span>`;
  if (save.error && state === "error") host.title = save.error;
  else host.removeAttribute("title");
}

function executableSelection() {
  if (!app.store) return [];
  return app.selection.nodeIds.filter((id) => app.catalog?.get(app.store.getNode(id)?.type)?.executable);
}

function renderRunControls() {
  if (!shell) return;
  const run = $("run");
  const more = $("run-menu");
  const stop = $("stop");
  const active = env.runActive;
  const starting = app.run.starting;
  const unavailable = !app.workflow || env.readOnly;
  run.disabled = unavailable || active || starting;
  more.disabled = unavailable || active || starting;
  run.classList.toggle("is-busy", active || starting);
  run.querySelector(".cv-icon")?.remove();
  run.insertAdjacentHTML("afterbegin", active || starting ? '<span class="cv-spinner cv-icon" aria-hidden="true"></span>' : icon("play", { size: 13 }));
  $("run-label").textContent = active ? "Running…" : starting ? "Starting…" : "Run";
  stop.hidden = !active || env.readOnly;
  stop.disabled = app.run.stopping;
  if (unavailable && env.readOnly && app.workflow) run.title = "This canvas is view only";
  else run.title = `Run everything (${MOD}+Enter)`;
}

function openRunMenu(anchor) {
  if (!app.workflow) return;
  const selected = executableSelection();
  const rect = anchor.getBoundingClientRect();
  const bounds = shell.getBoundingClientRect();
  anchor.setAttribute("aria-expanded", "true");
  menu.show({
    x: rect.right - bounds.left - 260,
    y: rect.bottom - bounds.top + 6,
    anchor,
    minWidth: 260,
    onClose: () => anchor.setAttribute("aria-expanded", "false"),
    items: [
      { label: "Run all", description: "Unchanged nodes reuse their earlier results", icon: "play", shortcut: `${MOD}↵`, onSelect: () => startRun({}) },
      {
        label: selected.length ? `Run selected (${selected.length})` : "Run selected",
        description: selected.length ? "Regenerates these, plus whatever feeds them" : "Select nodes on the canvas first",
        icon: "cursor",
        disabled: !selected.length,
        onSelect: () => startRun({ nodeIds: selected }),
      },
      { separator: true },
      { label: "Re-run all (ignore cached)", description: "Generates every node again, charging again", icon: "refresh", onSelect: () => startRun({ force: true }) },
    ],
  });
}

async function openWorkflowMenu(anchor) {
  if (!app.projectId) return;
  const rect = anchor.getBoundingClientRect();
  const bounds = shell.getBoundingClientRect();
  const readOnly = env.readOnly;
  const items = app.workflows.map((workflow) => ({
    label: `${workflow.id === app.workflowId ? "✓ " : ""}${workflow.name || "Untitled canvas"}`,
    description: [
      `${workflow.node_count ?? 0} node${workflow.node_count === 1 ? "" : "s"}`,
      workflow.updated_at ? `edited ${relativeTime(workflow.updated_at)}` : "",
      workflow.last_run?.status ? `last run ${String(workflow.last_run.status).toLowerCase()}` : "",
    ].filter(Boolean).join(" · "),
    icon: "grid",
    onSelect: () => {
      if (workflow.id !== app.workflowId) switchWorkflow(workflow.id);
    },
  }));
  items.push({ separator: true });
  items.push({ label: "New canvas", icon: "plus", disabled: readOnly, onSelect: () => createWorkflowFlow() });
  if (app.workflow) {
    items.push(
      { label: "Rename this canvas", icon: "pencil", disabled: readOnly, onSelect: () => renameWorkflowFlow() },
      { label: "Delete this canvas", icon: "trash", danger: true, disabled: readOnly, onSelect: () => deleteWorkflowFlow() },
    );
  }
  anchor.setAttribute("aria-expanded", "true");
  menu.show({
    x: rect.left - bounds.left,
    y: rect.bottom - bounds.top + 6,
    anchor,
    minWidth: 300,
    title: app.project?.name ? `Canvases in ${app.project.name}` : "Canvases",
    search: app.workflows.length > 6 ? { placeholder: "Find a canvas" } : null,
    items,
    onClose: () => anchor.setAttribute("aria-expanded", "false"),
  });
}

/**
 * Before leaving the open canvas: save what is pending, and when that fails
 * with edits still unsaved, let the user decide instead of dropping them.
 */
async function confirmLeave() {
  const saved = await flushSave();
  const save = app.save;
  if (saved || !save || env.readOnly) return true;
  if (!save.graphDirty && save.pendingName === null) return true;
  return confirmSheet({
    title: "Leave without saving?",
    message: save.conflict
      ? "This canvas was changed somewhere else, so your latest edits here were not saved. If you continue, they are lost."
      : "Your latest changes to this canvas could not be saved. If you continue, they are lost.",
    confirmLabel: "Continue anyway",
    danger: true,
  });
}

async function switchWorkflow(workflowId) {
  if (!(await confirmLeave())) return;
  openWorkflow(workflowId).catch(reportError);
}

/* ================================================================== project and canvas flows */

async function createProjectFlow() {
  const epoch = app.epoch;
  if (!(await confirmLeave())) return;
  const created = await promptSheet({
    title: "New project",
    message: "Give it a name your team will recognise. It opens with a canvas ready to run.",
    label: "Project name",
    value: "Untitled project",
    confirmLabel: "Create project",
    onSubmit: (name) => api.createProject(name),
  });
  if (!created || staleEpoch(epoch)) return;
  try {
    const projects = await api.listProjects();
    if (staleEpoch(epoch)) return;
    app.projects = Array.isArray(projects) ? projects : [created];
  } catch (_error) {
    app.projects = [...app.projects, created];
  }
  renderProjectSelect();
  await selectProject(created.id, { epoch });
  toast("Project created");
}

async function createWorkflowFlow() {
  if (!app.projectId || env.readOnly) return;
  const projectId = app.projectId;
  const epoch = app.epoch;
  if (!(await confirmLeave())) return;
  const created = await promptSheet({
    title: "New canvas",
    label: "Canvas name",
    value: "Untitled canvas",
    confirmLabel: "Create canvas",
    onSubmit: (name) => api.createWorkflow(projectId, { name }),
  });
  if (!created || staleEpoch(epoch) || app.projectId !== projectId) return;
  app.workflows = [summaryOf(created), ...app.workflows.filter((item) => item.id !== created.id)];
  await openWorkflow(created.id, { epoch });
}

async function renameWorkflowFlow() {
  if (!app.workflow || env.readOnly) return;
  const name = await promptSheet({
    title: "Rename canvas",
    label: "Canvas name",
    value: app.workflow.name || "",
    confirmLabel: "Rename",
    onSubmit: async (value) => value,
  });
  if (name) renameWorkflow(name);
}

function renameWorkflow(value) {
  if (!app.workflow || env.readOnly || !app.save) return;
  const name = String(value || "").replace(/\s+/g, " ").trim().slice(0, 200);
  if (!name || name === app.workflow.name) {
    renderTopbar();
    return;
  }
  app.workflow.name = name;
  const summary = app.workflows.find((item) => item.id === app.workflowId);
  if (summary) summary.name = name;
  app.save.pendingName = name;
  app.save.seq += 1;
  setSaveState("dirty");
  renderTopbar();
  inspector.updateWorkflowFacts();
  scheduleSave(0);
}

async function deleteWorkflowFlow() {
  if (!app.workflow || env.readOnly) return;
  const workflow = app.workflow;
  const epoch = app.epoch;
  const confirmed = await confirmSheet({
    title: `Delete “${workflow.name || "Untitled canvas"}”?`,
    message: "The canvas and its layout are removed. Anything it already generated stays in Productions, and a run in progress is stopped.",
    confirmLabel: "Delete canvas",
    danger: true,
  });
  if (!confirmed || staleEpoch(epoch) || app.workflowId !== workflow.id) return;
  try {
    await api.deleteWorkflow(workflow.id);
  } catch (error) {
    reportError(error);
    return;
  }
  if (staleEpoch(epoch)) return;
  app.workflows = app.workflows.filter((item) => item.id !== workflow.id);
  closeWorkflow();
  toast("Canvas deleted");
  if (app.workflows.length) await openWorkflow(app.workflows[0].id, { epoch });
  else {
    setStage("no-canvas");
    navigate("/app", { replace: true });
  }
}

/* ================================================================== document changes and autosave */

function onDocumentChange(change) {
  inspector.onStoreChange(change);
  if (change.origin === "load" || change.kind === "history") return;
  if (change.kind === "viewport") {
    markDirty({ viewportOnly: true });
    return;
  }
  if (!change.docChanged) return;
  markDirty({ viewportOnly: false });
  revalidate(change);
  if (change.kind === "remove" || change.kind === "replace") renderRunControls();
  inspector.updateWorkflowFacts();
}

function markDirty({ viewportOnly }) {
  const save = app.save;
  if (!save || env.readOnly || save.conflict) return;
  save.seq += 1;
  if (viewportOnly) {
    save.viewportDirty = true;
    scheduleSave(save.graphDirty || save.pendingName !== null ? SAVE_DEBOUNCE : VIEWPORT_DEBOUNCE);
    return;
  }
  save.graphDirty = true;
  if (save.state !== "saving") setSaveState("dirty");
  scheduleSave(SAVE_DEBOUNCE);
}

function scheduleSave(delay) {
  const save = app.save;
  if (!save) return;
  clearTimeout(save.timer);
  save.timer = setTimeout(() => runSave(), delay);
}

function runSave({ keepalive = false } = {}) {
  const save = app.save;
  const workflowId = app.workflowId;
  const store = app.store;
  const epoch = app.epoch;
  if (!save || !workflowId || !store || env.readOnly || save.conflict) return Promise.resolve(false);
  clearTimeout(save.timer);
  save.timer = null;
  if (save.inFlight) {
    save.again = true;
    return save.inFlight;
  }
  if (!save.graphDirty && !save.viewportDirty && save.pendingName === null) return Promise.resolve(true);
  const seqAtStart = save.seq;
  const body = { base_version: save.version };
  if (save.graphDirty || save.viewportDirty) body.graph = store.toJSON();
  if (save.pendingName !== null) body.name = save.pendingName;
  const visible = save.graphDirty || save.pendingName !== null;
  if (visible) setSaveState("saving");
  save.inFlight = (async () => {
    try {
      const result = await api.saveWorkflow(workflowId, body, { keepalive: keepalive && JSON.stringify(body).length < 60_000 });
      if (staleEpoch(epoch) || app.save !== save) return false;
      save.version = result.version;
      app.workflow.version = result.version;
      app.workflow.updated_at = result.updated_at;
      if (body.name !== undefined && save.pendingName === body.name) save.pendingName = null;
      if (save.seq === seqAtStart) {
        save.graphDirty = false;
        save.viewportDirty = false;
      }
      save.error = null;
      save.failures = 0;
      const summary = app.workflows.find((item) => item.id === workflowId);
      if (summary) {
        Object.assign(summary, { version: result.version, updated_at: result.updated_at, node_count: store.nodes.length });
        if (result.name) summary.name = result.name;
      }
      if (result.name && document.activeElement !== $("wf-name")) {
        app.workflow.name = result.name;
        $("wf-name").value = result.name;
      }
      setSaveState(save.graphDirty || save.pendingName !== null ? "dirty" : "saved");
      inspector.updateWorkflowFacts();
      return !save.graphDirty && !save.viewportDirty && save.pendingName === null;
    } catch (error) {
      if (staleEpoch(epoch) || app.save !== save) return false;
      if (error.status === 401) return false;
      if (error.status === 409) {
        save.conflict = true;
        setSaveState("conflict");
        showConflict();
        return false;
      }
      if (error.status === 403) {
        setReadOnly("You can view this canvas, but saving needs editor access to the workspace.");
        return false;
      }
      if (error.status === 404) {
        save.conflict = true;
        setSaveState("conflict");
        showBanner({ kind: "deleted", tone: "danger", message: "This canvas was deleted somewhere else. Your changes can't be saved to it.", actions: [
          { label: "Copy my nodes", onClick: copyWholeCanvas },
          { label: "Open another canvas", primary: true, onClick: () => selectProject(app.projectId) },
        ] });
        return false;
      }
      save.error = error.message;
      setSaveState("error");
      if (error.status === 422) {
        applyIssues(error.detail?.errors || [], { reveal: false });
        toast(`Not saved: ${error.message}`, { tone: "danger" });
        return false;
      }
      save.failures += 1;
      if (save.failures <= 3) {
        clearTimeout(save.retryTimer);
        save.retryTimer = setTimeout(() => runSave(), 4000 * save.failures);
      }
      return false;
    } finally {
      save.inFlight = null;
      if (save.again && app.save === save && !save.conflict) {
        save.again = false;
        if (save.graphDirty || save.viewportDirty || save.pendingName !== null) scheduleSave(0);
      }
    }
  })();
  return save.inFlight;
}

/** Save everything pending now. Resolves true when nothing is left unsaved. */
async function flushSave() {
  const save = app.save;
  if (!save || env.readOnly) return true;
  if (save.conflict) return false;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (save.inFlight) await save.inFlight;
    if (app.save !== save || save.conflict) return false;
    if (!save.graphDirty && !save.viewportDirty && save.pendingName === null) return true;
    const ok = await runSave();
    if (!ok && (save.error || save.conflict)) return false;
  }
  return !save.graphDirty && !save.viewportDirty && save.pendingName === null;
}

async function saveNow() {
  if (!app.workflow) return;
  if (env.readOnly) {
    toast("This canvas is view only.");
    return;
  }
  const save = app.save;
  if (save) {
    save.failures = 0;
    save.error = null;
  }
  const ok = await flushSave();
  if (ok) toast("Saved");
  else if (app.save?.error) toast(`Not saved: ${app.save.error}`, { tone: "danger" });
}

function showConflict() {
  showBanner({
    kind: "conflict",
    tone: "warn",
    message: "This canvas was changed somewhere else. Reload to get the latest version — your unsaved edits here will be replaced.",
    actions: [
      { label: "Copy my nodes", onClick: copyWholeCanvas },
      { label: "Reload", primary: true, onClick: () => reloadWorkflow() },
    ],
  });
}

function copyWholeCanvas() {
  if (!app.store) return;
  const payload = app.store.copy(app.store.nodes.map((node) => node.id));
  if (!payload) return;
  app.clipboard = payload;
  navigator.clipboard?.writeText?.(JSON.stringify(payload)).catch(() => null);
  toast(`Copied ${payload.nodes.length} node${payload.nodes.length === 1 ? "" : "s"}. Paste them into any canvas with ${MOD}+V.`);
}

async function reloadWorkflow() {
  const workflowId = app.workflowId;
  if (!workflowId) return;
  hideBanner("conflict");
  await openWorkflow(workflowId);
}

function setReadOnly(message) {
  env.readOnly = true;
  palette.setReadOnly(true);
  if (app.save) {
    clearTimeout(app.save.timer);
    app.save.graphDirty = false;
    app.save.viewportDirty = false;
  }
  editor.cards.refreshContext();
  editor.updateHistoryButtons();
  inspector.render();
  renderTopbar();
  showBanner({ kind: "readonly", tone: "warn", message });
}

/* ================================================================== validation issues */

function revalidate(change) {
  if (!env.errors.size || !app.store) return;
  const touched = new Set([...(change.nodeIds || []), ...(change.touchedNodeIds || [])]);
  const refresh = [];
  for (const nodeId of [...env.errors.keys()]) {
    if (!app.store.getNode(nodeId)) {
      env.errors.delete(nodeId);
      continue;
    }
    if (change.kind !== "replace" && change.kind !== "edges" && !touched.has(nodeId)) continue;
    const issues = validateForRun(app.store.doc, app.catalog, [nodeId]).filter((issue) => issue.node_id === nodeId);
    if (issues.length) env.errors.set(nodeId, issues);
    else env.errors.delete(nodeId);
    refresh.push(nodeId);
  }
  if (refresh.length) {
    editor.cards.refresh(refresh);
    inspector.refreshErrors(refresh);
  }
}

function applyIssues(issues, { reveal = true } = {}) {
  const previous = [...env.errors.keys()];
  env.errors = new Map();
  for (const issue of issues || []) {
    if (!issue?.node_id || !app.store?.getNode(issue.node_id)) continue;
    if (!env.errors.has(issue.node_id)) env.errors.set(issue.node_id, []);
    env.errors.get(issue.node_id).push(issue);
  }
  const touched = [...new Set([...previous, ...env.errors.keys()])];
  editor.cards.refresh(touched);
  inspector.refreshErrors(touched);
  const first = (issues || []).find((issue) => issue?.node_id && app.store?.getNode(issue.node_id));
  if (reveal && first) editor.revealNode(first.node_id);
}

/* ================================================================== runs */

async function startRun({ nodeIds = null, force = undefined } = {}) {
  if (!app.workflow || !app.store) return;
  if (env.readOnly) {
    toast("This canvas is view only.");
    return;
  }
  if (env.runActive) {
    toast("This canvas is already running. Stop it, or wait for it to finish.");
    return;
  }
  if (app.run.starting) return;
  const store = app.store;
  const scope = runScope(store.doc, app.catalog, nodeIds);
  if (!scope.scope.length) {
    toast(nodeIds?.length ? "Notes don't run. Select a node that makes something." : "Add a node to run first.");
    return;
  }
  const issues = validateForRun(store.doc, app.catalog, nodeIds);
  if (issues.length) {
    applyIssues(issues);
    const count = new Set(issues.map((issue) => issue.node_id)).size;
    toast(`${count === 1 ? "One node needs" : `${count} nodes need`} attention before running: ${issues[0].message}.`, { tone: "danger" });
    return;
  }
  const epoch = app.epoch;
  const workflowId = app.workflowId;
  app.run.starting = true;
  renderRunControls();
  try {
    const saved = await flushSave();
    if (staleEpoch(epoch) || app.workflowId !== workflowId) return;
    if (!saved) {
      toast(app.save?.conflict
        ? "Reload the canvas before running — it was changed somewhere else."
        : "The canvas could not be saved, so it did not run. Try again.", { tone: "danger" });
      return;
    }
    const body = { base_version: app.save.version };
    if (nodeIds?.length) body.node_ids = scope.targets;
    if (force !== undefined) body.force = force;
    const run = await api.startRun(workflowId, body);
    if (staleEpoch(epoch) || app.workflowId !== workflowId) return;
    if (env.errors.size) applyIssues([], { reveal: false });
    app.run.watching = run.id;
    applyRun(run);
  } catch (error) {
    if (staleEpoch(epoch) || app.workflowId !== workflowId) return;
    await handleRunError(error);
  } finally {
    if (!staleEpoch(epoch)) {
      app.run.starting = false;
      renderRunControls();
    }
  }
}

async function handleRunError(error) {
  const code = error.reasonCode;
  if (error.status === 401) return;
  if (error.status === 409 && code === "WORKFLOW_RUN_ACTIVE") {
    toast("This canvas is already running. Showing that run.");
    if (error.detail?.run_id) {
      try {
        const run = await api.run(error.detail.run_id);
        app.run.watching = run.id;
        applyRun(run);
      } catch (_failure) { /* the next poll finds it */ }
    }
    return;
  }
  if (error.status === 409 && code === "WORKFLOW_VERSION_CONFLICT") {
    if (app.save) app.save.conflict = true;
    setSaveState("conflict");
    showConflict();
    return;
  }
  if (error.status === 409 && code === "WORKFLOW_NOTHING_TO_RUN") {
    toast(error.message.charAt(0).toUpperCase() + error.message.slice(1));
    return;
  }
  if (error.status === 422 && Array.isArray(error.detail?.errors)) {
    applyIssues(error.detail.errors);
    toast(`Not run: ${error.message}`, { tone: "danger" });
    return;
  }
  if (error.status === 402) {
    toast("You're out of credits for this run.", { tone: "danger", action: { label: "Top up", onClick: openWallet } });
    return;
  }
  if (error.status === 403) {
    toast("You don't have permission to run this canvas.", { tone: "danger" });
    return;
  }
  reportError(error);
}

function applyRun(run) {
  if (!run || run.workflow_id !== app.workflowId || !editor.store) return;
  const wasActive = env.runActive;
  const previous = app.run.active;
  app.run.active = run;
  env.runActive = ACTIVE_RUN.has(run.status);
  const changed = [];
  for (const nodeRun of run.node_runs || []) {
    env.results.set(nodeRun.node_id, nodeRun);
    if (nodeRun.status === "SUCCEEDED" || nodeRun.status === "CACHED") env.lastOutputs.set(nodeRun.node_id, nodeRun);
    changed.push(nodeRun.node_id);
  }
  if (wasActive !== env.runActive) editor.cards.refresh();
  else editor.cards.refresh(changed);
  inspector.refreshResults(changed);
  editor.schedule("minimap");
  renderRunControls();
  if (env.runActive) {
    ensurePolling(run.id);
    return;
  }
  stopPolling();
  const watched = app.run.watching === run.id || (previous && previous.id === run.id && ACTIVE_RUN.has(previous.status));
  if (watched) finishRun(run);
}

function finishRun(run) {
  app.run.watching = null;
  const counts = run.counts || {};
  const failedNodes = (run.node_runs || []).filter((item) => item.status === "FAILED");
  const outOfCredits = failedNodes.some((item) => item.error_code === "INSUFFICIENT_CREDITS" || item.job?.error_code === "INSUFFICIENT_CREDITS");
  if (run.status === "SUCCEEDED") {
    const made = counts.SUCCEEDED || 0;
    const reused = counts.CACHED || 0;
    toast(reused ? `Run finished · ${made} done, ${reused} reused` : "Run finished", { tone: "ok" });
  } else if (run.status === "FAILED") {
    const first = failedNodes[0];
    const title = first ? nodeTitle(app.store?.getNode(first.node_id), app.catalog?.get(first.node_type)) : "";
    toast(first ? `${title} failed: ${first.error_message || "see the node for details"}` : run.error_message || "The run did not finish.", {
      tone: "danger",
      action: outOfCredits ? { label: "Top up", onClick: openWallet } : (first ? { label: "Show", onClick: () => editor.revealNode(first.node_id) } : null),
    });
  } else if (run.status === "CANCELLED") {
    toast("Run stopped");
  }
  loadBilling();
  loadRuns();
  const summary = app.workflows.find((item) => item.id === run.workflow_id);
  if (summary) summary.last_run = { id: run.id, status: run.status, finished_at: run.finished_at };
}

function ensurePolling(runId) {
  if (app.run.poll?.runId === runId) return;
  stopPolling();
  if (!app.active) return;
  const poll = { runId, startedAt: Date.now(), timer: null, failures: 0, epoch: app.epoch, workflowId: app.workflowId };
  app.run.poll = poll;
  poll.timer = setTimeout(() => pollOnce(poll), POLL_FAST);
}

async function pollOnce(poll) {
  if (app.run.poll !== poll || !app.active) return;
  try {
    const run = await api.run(poll.runId);
    if (app.run.poll !== poll || staleEpoch(poll.epoch) || app.workflowId !== poll.workflowId) return;
    poll.failures = 0;
    applyRun(run);
    if (app.run.poll === poll && ACTIVE_RUN.has(run.status)) {
      const delay = Date.now() - poll.startedAt > POLL_SLOW_AFTER ? POLL_SLOW : POLL_FAST;
      poll.timer = setTimeout(() => pollOnce(poll), delay);
    }
  } catch (error) {
    if (app.run.poll !== poll) return;
    if (error.status === 401) {
      stopPolling();
      return;
    }
    if (error.status === 404 || error.status === 403) {
      stopPolling();
      app.run.active = null;
      env.runActive = false;
      editor.cards.refresh();
      renderRunControls();
      return;
    }
    poll.failures += 1;
    poll.timer = setTimeout(() => pollOnce(poll), Math.min(15_000, POLL_SLOW * poll.failures));
  }
}

function stopPolling() {
  if (app.run?.poll) clearTimeout(app.run.poll.timer);
  if (app.run) app.run.poll = null;
}

async function stopRun() {
  const run = app.run.active;
  if (!run || !ACTIVE_RUN.has(run.status) || app.run.stopping) return;
  const epoch = app.epoch;
  app.run.stopping = true;
  app.run.watching = run.id;
  renderRunControls();
  try {
    const stopped = await api.cancelRun(run.id);
    if (staleEpoch(epoch)) return;
    applyRun(stopped);
  } catch (error) {
    if (!staleEpoch(epoch)) reportError(error);
  } finally {
    if (!staleEpoch(epoch)) {
      app.run.stopping = false;
      renderRunControls();
    }
  }
}

/* ================================================================== images */

async function uploadNodeImage(nodeId, rawFile) {
  if (env.readOnly || !app.store) return;
  const file = nameClipboardImage(rawFile);
  const problem = imageFileProblem(file);
  if (problem) {
    toast(problem, { tone: "danger" });
    return;
  }
  const store = app.store;
  const epoch = app.epoch;
  const projectId = app.projectId;
  env.uploads.set(nodeId, { status: "uploading", name: file.name, file });
  refreshUploadViews(nodeId);
  try {
    const asset = await uploadImage(projectId, file);
    if (staleEpoch(epoch) || app.store !== store) return;
    env.uploads.delete(nodeId);
    if (store.getNode(nodeId)) {
      store.updateNodeData(nodeId, { asset_id: asset.id });
      const remaining = (env.errors.get(nodeId) || []).filter((issue) => issue.param !== "asset_id");
      if (remaining.length) env.errors.set(nodeId, remaining);
      else env.errors.delete(nodeId);
    }
    refreshUploadViews(nodeId);
  } catch (error) {
    if (staleEpoch(epoch) || app.store !== store || error.status === 401) return;
    env.uploads.set(nodeId, { status: "error", name: file.name, file, message: error.message });
    refreshUploadViews(nodeId);
  }
}

function refreshUploadViews(nodeId) {
  const record = editor.cards.get(nodeId);
  if (record) {
    editor.cards.renderBody(record, { force: true });
    editor.cards.renderErrors(record);
  }
  if (app.selection.nodeIds.length === 1 && app.selection.nodeIds[0] === nodeId) inspector.render();
}

function addImageFiles(files, at) {
  if (env.readOnly || !app.store || !app.catalog?.get("image_input")) return;
  const images = [...files].slice(0, 12).map(nameClipboardImage);
  const accepted = [];
  for (const file of images) {
    const problem = imageFileProblem(file);
    if (problem) toast(problem, { tone: "danger" });
    else accepted.push(file);
  }
  if (!accepted.length) return;
  const store = app.store;
  const created = [];
  store.beginBatch();
  try {
    accepted.forEach((file, index) => {
      const node = store.addNode("image_input", { x: (at?.x ?? 0) - 150 + index * 36, y: (at?.y ?? 0) - 60 + index * 36 }, {}, {
        label: file.name.replace(/\.[^.]+$/, "").slice(0, 80),
      });
      if (node) created.push([node.id, file]);
    });
  } finally {
    store.endBatch();
  }
  if (!created.length) {
    if (store.lastError) toast(store.lastError);
    return;
  }
  editor.select(created.map(([id]) => id));
  for (const [id, file] of created) uploadNodeImage(id, file);
}

/* ================================================================== wallet, connections, viewer */

function openWallet() {
  announceWorkspace(true);
  const button = document.getElementById("walletBtn");
  if (button) button.click();
  else toast("Top-ups are not available right now.");
}

function openConnections({ capability = null } = {}) {
  if (!app.workspaceId) {
    toast("Open a project first.");
    return;
  }
  connectionsDialog.open({ workspaceId: app.workspaceId, capability });
}

function ensureViewer() {
  if (viewer) return viewer;
  const dialog = document.createElement("dialog");
  dialog.className = "sheet sheet-wide cv-sheet cv-viewer";
  dialog.setAttribute("aria-labelledby", "cv-viewer-title");
  dialog.innerHTML = `
    <div class="sheet-body">
      <div class="cv-viewer-head"><h2 id="cv-viewer-title"></h2><button type="button" class="cv-icon-btn" data-cv-viewer-close aria-label="Close">${icon("x", { size: 16 })}</button></div>
      <div class="cv-viewer-body"></div>
      <div class="sheet-actions"><button type="button" class="btn btn-tertiary" data-cv-viewer-copy hidden>${icon("copy", { size: 14 })}Copy text</button><button type="button" class="btn btn-secondary" data-cv-viewer-close>Close</button></div>
    </div>`;
  document.body.append(dialog);
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog || event.target.closest("[data-cv-viewer-close]")) dialog.close();
    if (event.target.closest("[data-cv-viewer-copy]")) env.actions.copyText(dialog.dataset.text || "");
  });
  dialog.addEventListener("close", () => {
    dialog.querySelector(".cv-viewer-body").innerHTML = "";
    dialog.dataset.text = "";
  });
  viewer = dialog;
  return viewer;
}

async function openViewer({ assetId = null, kind = "image", title = "", text = null }) {
  const dialog = ensureViewer();
  const body = dialog.querySelector(".cv-viewer-body");
  dialog.querySelector("#cv-viewer-title").textContent = title || (kind === "text" ? "Output" : "Preview");
  const copy = dialog.querySelector("[data-cv-viewer-copy]");
  copy.hidden = kind !== "text";
  if (kind === "text") {
    dialog.dataset.text = text || "";
    body.className = "cv-viewer-body is-text";
    body.innerHTML = `<pre class="cv-viewer-text">${escapeHTML(text || "")}</pre>`;
  } else {
    body.className = "cv-viewer-body media-viewer";
    body.setAttribute("data-surface", "dark");
    body.innerHTML = '<span class="cv-spinner is-large" aria-hidden="true"></span>';
  }
  if (!dialog.open) dialog.showModal();
  if (kind === "text" || !assetId) return;
  const epoch = app.epoch;
  const resolved = await media.media(assetId);
  if (!dialog.open || staleEpoch(epoch)) return;
  if (!resolved) {
    body.innerHTML = `<p class="cv-viewer-missing">${icon("alert", { size: 16 })}The media could not be loaded.</p>`;
    return;
  }
  if (kind === "video" || resolved.mime.startsWith("video/")) {
    body.innerHTML = "";
    const video = document.createElement("video");
    video.controls = true;
    video.autoplay = true;
    video.playsInline = true;
    video.src = resolved.url;
    body.append(video);
  } else {
    body.innerHTML = "";
    const image = document.createElement("img");
    image.alt = title || "Generated image";
    image.src = resolved.url;
    body.append(image);
  }
}

/* ================================================================== start */

if (shell) boot();
