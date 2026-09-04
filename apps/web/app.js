/**
 * Best Shiny — application shell.
 *
 * The backend contract is unchanged: same endpoints, same payloads, same
 * idempotency and credit rules. What changed is the shape of the surface —
 * one object-centric shell (sidebar / canvas / inspector / action bar) instead
 * of three pages of stacked forms.
 */
import { currentRoute, isAdminRoute, navigate, onRoute, setAuth } from "./router.js";

const API = window.AI_DIRECTOR_API
  || (location.hostname === "127.0.0.1" && location.port === "18081"
    ? "http://127.0.0.1:18080"
    : "/api");
const SUBMISSION_STORAGE_KEY = "aiDirectorPendingSubmissions";
const CSRF_COOKIE_NAME = "ai_director_csrf";
sessionStorage.removeItem("aiDirectorAccessToken");

function cookieValue(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie.split("; ").find((entry) => entry.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

function csrfHeaders(method = "GET", headers = {}) {
  const result = { ...headers };
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method.toUpperCase())) {
    const token = cookieValue(CSRF_COOKIE_NAME);
    if (token) result["X-CSRF-Token"] = token;
  }
  return result;
}

function restoreSubmissions() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(SUBMISSION_STORAGE_KEY) || "{}");
    const cutoff = Date.now() - (24 * 60 * 60 * 1000);
    return Object.fromEntries(["passenger", "shot"].map((slot) => {
      const item = saved[slot];
      const usable = item?.key && item?.fingerprint && Number(item.createdAt || 0) >= cutoff;
      return [slot, usable ? { ...item, inFlight: false, retryable: true } : null];
    }));
  } catch (_error) {
    sessionStorage.removeItem(SUBMISSION_STORAGE_KEY);
    return { passenger: null, shot: null };
  }
}

const state = {
  projects: [], project: null, episode: null, shot: null, candidates: [], characters: [],
  episodes: [],               // rich strip rows from /v1/projects/{id}/episodes
  creative: { sessions: [], session: null, beatEdits: {}, editingBrief: false, editingScreenplay: false, revealedTurn: null, revealedScreenplay: null, thinking: null },
  continuation: { mode: "CONTINUOUS", view: null },
  selectedCharacterId: null, page: "create", passengerMedia: "image", passengerOriginal: null,
  passengerPrompts: { image: "", video: "" }, passengerJobs: { image: null, video: null },
  passengerReferenceUpload: null, modelProfiles: [], passengerModels: [],
  imageTiers: null,           // server truth from /v1/image-tiers; null = static fallback
  confirmedAssets: new Set(), logicalAssets: [],
  thumbCache: new Map(),      // media asset id -> resolved thumbnail object URL (or null)
  assetMediaIds: new Map(),   // canonical version id -> primary media asset id (or null)
  savingJobId: null,          // job the save-to-project dialog is acting on
  authUser: null,
  authMode: "login", passengerPreviewObjectUrl: null,
  styleLock: null,
  submissions: restoreSubmissions(),
  operations: { providers: [], skills: [], job: null },
  jobs: new Map(),            // job id -> last known job view, for Productions
  jobFilter: "all",
  selectedJobId: null,
  credits: null,
};

const $ = (id) => document.getElementById(id);
const escapeHTML = (value = "") => String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);

/** Write only if the node exists. The admin console owns some of these
 *  surfaces now, and app.js must not throw when they are not in this shell. */
const setText = (id, value) => { const node = $(id); if (node) node.textContent = value; };

/** Bind only if the node exists, so a shell change can never kill the module. */
function on(id, event, handler) {
  const node = $(id);
  if (node) node.addEventListener(event, handler);
}
const guard = (fn) => (...args) => Promise.resolve()
  .then(() => fn(...args))
  .catch((error) => toast(error.message));

const simpleLabel = (value) => ({
  NEW: "Queued", RESERVED: "Reserved", DRAFT: "Draft", PLANNED: "Planned", READY: "Ready",
  COMPILED: "Shots built", ACTION: "Action", DIALOGUE: "Dialogue", MEDIUM: "Medium",
  CLOSE_UP: "Close-up", WIDE: "Wide", EXTREME_CLOSE_UP: "Extreme close-up",
  COMMERCIAL_HERO: "Commercial hero",
  QUEUED: "Queued", SUBMITTED: "Submitted", RUNNING: "Generating", RETRY_WAIT: "Waiting to retry",
  COMPLETED: "Completed", CANCELLED: "Cancelled", WORKER_NEEDS_USER_ACTION: "Needs your action",
  GENERATING: "Generating",
  VALIDATING: "Checking", PASSED: "Passed", SOFT_FAILED: "Needs a fix", HARD_FAILED: "Rejected",
  PASS: "Passed", SOFT_FAIL: "Needs a fix", HARD_FAIL: "Rejected",
  USER_REVIEW_REQUIRED: "Needs your review", COMMITTED: "Approved", REJECTED: "Not used",
  FAILED: "Failed",
  NONE: "Fresh start", PREVIOUS_END_FRAME: "Carries the last frame",
  REFERENCE_FRAME: "Carries a reference frame",
  HARD_CONTINUITY: "Must match exactly", HYBRID: "Last frame + reference",
  RE_ANCHOR: "Re-establishes character and location",
  IN_PRODUCTION: "In production", PROPOSED: "Proposed", APPROVED: "Approved",
  LOCKED: "Locked", SUPERSEDED: "Superseded", PENDING: "Pending",
  CAMERA_AXIS_CHANGE: "Crosses the axis", SCENE_CHANGE: "Scene changes",
  TIMELINE_JUMP: "Time jump",
  LOW_PREVIOUS_FRAME_QUALITY: "Previous end frame is soft",
  LOW_PREVIOUS_FACE_VISIBILITY: "Face unclear in previous shot",
  IDENTITY_DRIFT_RISK: "Identity may drift", ACTION_DISCONTINUITY: "Action does not join",
  HIGH_CONTINUITY_RISK: "High continuity risk", SAME_SCENE: "Same scene",
  ACTION_CHAIN_CONTINUES: "Action continues",
  USABLE_END_FRAME: "Previous end frame is usable",
  MODERATE_CAMERA_OR_BLOCKING_CHANGE: "Camera or blocking moved",
  // "What this shot is built from" — the answer to a question the user asked,
  // not the name of a pipeline. TEXT_TO_VIDEO etc. stay the wire values.
  TEXT_TO_VIDEO: "Your words", IMAGE_TO_VIDEO: "A still image",
  CONTINUE_I2V: "The last frame", CONTINUE_V2V: "The last clip",
  HYBRID_REFERENCE: "The last frame plus a reference",
  REANCHOR_CHARACTER: "A fresh look at the character", REANCHOR_SCENE: "A fresh look at the location",
  REANCHOR_FULL: "A fresh look at character and location",
  START_END_FRAME: "A first and last frame", REFERENCE_TO_VIDEO: "Your references",
  // How an episode opens onto the one before it ([data-continuation-mode]).
  CONTINUOUS: "Continues the scene", TIME_JUMP: "Time jump", LOCATION_CHANGE: "New location",
  portrait: "Portrait", beauty_fashion: "Beauty & fashion", product: "Product",
  commercial: "Commercial", scene_concept: "Scene concept",
  reference_character_regeneration: "Identity preserving",
  CHARACTER: "Character", SCENE: "Scene", PRODUCT: "Product", PROP: "Prop", WARDROBE: "Wardrobe",
  VEHICLE: "Vehicle", CREATURE: "Creature", VOICE: "Voice", STYLE: "Style", REFERENCE: "Reference",
  LOCATION: "Location", KEY_FRAME: "Key frame",
}[value] || value || "—");

/** "identity drift" -> "Identity drift". De-cased enums read as a sentence,
 *  never as SHOUTED_CODE, on any surface a creator sees. */
const sentenceCase = (text = "") => (text ? text.charAt(0).toUpperCase() + text.slice(1) : "");

/** Public names for the models the platform runs. Raw provider model IDs and
 *  version hashes are backend facts; the UI only ever shows these. The video
 *  rows are BestShiny route names on purpose — no vendor, no version number —
 *  so the catalogue reads as one product instead of a list of other people's
 *  brands. A route with no row here falls back to "BestShiny model", which is
 *  why a test asserts every user_visible video model id has a key. */
const MODEL_LABELS = {
  // Images.
  "doubao-seedream-5-0-260128": "Shiny",
  "NARWHAL": "Shinier",
  "openai/gpt-image-2": "Shiniest",
  // Video.
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
const friendlyModel = (modelId) => MODEL_LABELS[modelId] || (modelId ? "BestShiny model" : "—");

/** The Director page holds a provider string but never a model id, so it can
 *  only name the tier the route belongs to. Tier-only is the honest answer:
 *  naming a vendor there would promise a specific model we have not resolved. */
const PROVIDER_TIER = {
  seedance: "Shiny Motion", grok: "Shiny Motion",
  kling: "Shinier Motion", wan: "Shinier Motion",
  runway: "Shinier Motion", omni: "Shinier Motion",
  veo_official: "Shiniest Motion", google_flow: "Shiniest Motion",
};
const routeName = (provider) => PROVIDER_TIER[provider] || (provider ? "BestShiny model" : "—");

/** The quote the job's reservation was taken on, in USD. The provider-verified
 *  figure never reaches the browser — it is a billing internal. */
function jobCostUsd(job) {
  const value = Number(job.estimated_cost ?? 0);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

/** Credits for one job: the quoted credits column when the server sent it,
 *  otherwise derived from the USD cost at the platform rate. */
function jobCredits(job) {
  const quoted = Number(job.estimated_credits);
  if (Number.isFinite(quoted) && quoted > 0) return quoted;
  const cost = jobCostUsd(job);
  return cost > 0 ? Math.max(1, Math.ceil(cost / .01)) : 0;
}

/** The public image quality levels. The backend owns what each level runs on;
 *  the browser only ever sends the level's name. */
const IMAGE_TIERS = [
  { value: "shiny", stars: "\u2728", name: "Shiny", plan: "Free", tagline: "Fast creation, stable image generation" },
  { value: "shinier", stars: "\u2728\u2728", name: "Shinier", plan: "Pro", tagline: "Richer details and stronger visual expression" },
  { value: "shiniest", stars: "\u2728\u2728\u2728", name: "Shiniest", plan: "Pro", tagline: "Highest quality and finest visual detail" },
];

const isFreeWorkspace = () =>
  Boolean(state.authUser?.workspaces?.some((workspace) => workspace.plan_tier === "FREE"));

const humanizeCode = (code = "") => String(code).replaceAll("_", " ").toLowerCase();

/** Status → the four-colour vocabulary. Never red for "not configured". */
function statusTone(status) {
  if (["COMPLETED", "PASSED", "PASS", "COMMITTED", "READY"].includes(status)) return "is-ok";
  if (["RUNNING", "GENERATING", "SUBMITTED", "VALIDATING"].includes(status)) return "is-running";
  if (["QUEUED", "NEW", "RESERVED", "RETRY_WAIT"].includes(status)) return "is-queued";
  if (["FAILED", "HARD_FAILED", "HARD_FAIL"].includes(status)) return "is-danger";
  return "is-neutral";
}

const userUploadMediaType = (logicalAssetType) => ({
  CHARACTER: "CHARACTER_REFERENCE",
  SCENE: "LOCATION_REFERENCE",
  PROP: "PROP_REFERENCE",
}[logicalAssetType] || "REFERENCE");

function beginSubmission(slot, fingerprint) {
  const current = state.submissions[slot];
  if (current?.inFlight) return null;
  const retryKey = current?.retryable && current.fingerprint === fingerprint ? current.key : null;
  const nonce = globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const key = retryKey || `${slot}-${nonce}`;
  state.submissions[slot] = {
    fingerprint, key, inFlight: true, retryable: false, createdAt: Date.now(),
  };
  sessionStorage.setItem(SUBMISSION_STORAGE_KEY, JSON.stringify(state.submissions));
  return key;
}

function finishSubmission(slot, key, succeeded) {
  const current = state.submissions[slot];
  if (!current || current.key !== key) return;
  state.submissions[slot] = succeeded
    ? null
    : { ...current, inFlight: false, retryable: true };
  sessionStorage.setItem(SUBMISSION_STORAGE_KEY, JSON.stringify(state.submissions));
}

async function request(path, options = {}) {
  const method = options.method || "GET";
  const headers = csrfHeaders(method, { "Content-Type": "application/json", ...(options.headers || {}) });
  const response = await fetch(`${API}${path}`, { ...options, credentials: "include", headers });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    if (response.status === 401 && !path.startsWith("/api/auth/")) lockAuth();
    // The creative director answers refusals with {message, reason_code, ...};
    // other routes with a plain string. Both become a readable error.
    const body = detail.detail;
    const message = body && typeof body === "object"
      ? (body.message || JSON.stringify(body))
      : (body || `Request failed (${response.status})`);
    const error = new Error(message);
    error.status = response.status;
    error.detail = body;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

let toastTimer = null;
function toast(message) {
  if (!message) return;
  const node = $("toast");
  node.textContent = message;
  // A modal <dialog> lives in the top layer, where no z-index can reach it, so
  // a toast raised during a dialog flow would be invisible exactly when it
  // matters most — reporting that the dialog's action failed. Showing it as a
  // popover puts it in the top layer too.
  if (typeof node.showPopover === "function" && !node.matches(":popover-open")) {
    try { node.showPopover(); } catch (_error) { /* raced with another toast */ }
  }
  node.classList.add("show");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    node.classList.remove("show");
    if (typeof node.hidePopover === "function" && node.matches(":popover-open")) {
      try { node.hidePopover(); } catch (_error) { /* already dismissed */ }
    }
  }, 2800);
}

/* ============================================================
   Auth
   ============================================================ */
function lockAuth() {
  if (state.passengerPreviewObjectUrl) URL.revokeObjectURL(state.passengerPreviewObjectUrl);
  state.passengerPreviewObjectUrl = null;
  state.authUser = null;
  clearWorkspaceState();
  setAuthMode("login");
  $("authPassword").value = "";
  setAuth(null);
  if (currentRoute() === "/app") navigate("/login", { replace: true });
  window.dispatchEvent(new CustomEvent("ai-director:auth", { detail: null }));
}

function clearWorkspaceState() {
  state.projects = [];
  state.project = null;
  state.episode = null;
  state.shot = null;
  state.candidates = [];
  state.characters = [];
  state.logicalAssets = [];
  state.styleLock = null;
  state.passengerJobs = { image: null, video: null };
  state.passengerPrompts = { image: "", video: "" };
  state.passengerReferenceUpload = null;
  state.submissions = { passenger: null, shot: null };
  state.jobs.clear();
  state.selectedJobId = null;
  state.credits = null;
  state.imageTiers = null;
  state.savingJobId = null;
  state.assetMediaIds.clear();
  state.thumbCache.forEach((url) => { if (url) URL.revokeObjectURL(url); });
  state.thumbCache.clear();
  stopPassengerPolling();
  sessionStorage.removeItem(SUBMISSION_STORAGE_KEY);
  state.confirmedAssets.clear();
  $("projectSelect").innerHTML = '<option value="">No projects yet</option>';
  $("characterList").innerHTML = CHARACTERS_EMPTY;
  bindCharactersEmptyCta($("characterList"));
  $("passengerExistingAsset").innerHTML = '<option value="">Create a new asset</option>';
  $("manualExistingAsset").innerHTML = '<option value="">Create a new asset</option>';
  $("manualAssetFile").value = "";
  $("manualAssetStatus").textContent = "A character's master reference can also be updated from the Director inspector.";
  $("lockProjectStyleBtn").disabled = true;
  $("projectStyleLockStatus").textContent = "Make a style version the main reference first, then a project member can lock it explicitly.";
  $("passengerReference").value = "";
  $("passengerPrompt").value = "";
  $("scriptInput").value = "";
  $("rawPrompt").value = "";
  $("compiledPrompt").value = "";
  renderAssetRail();
  renderProductions();
  resetProductionView();
  renderPassengerJob(null);
}

function unlockAuth(user) {
  state.authUser = user;
  const name = user.display_name || user.email || "";
  $("accountName").textContent = name;
  $("userMenuEmail").textContent = user.email || "";
  $("userAvatar").textContent = (name.trim()[0] || "·").toUpperCase();
  $("adminNavBtn").hidden = !["ADMIN", "SUPER_ADMIN"].includes(user?.platform_role);
  $("reconcileJobBtn").hidden = !isWorkspaceAdmin(user);
  setAuth(user);
  window.dispatchEvent(new CustomEvent("ai-director:auth", { detail: user }));
}

function isWorkspaceAdmin(user) {
  return (user?.workspaces || []).some((workspace) => ["OWNER", "ADMIN"].includes(workspace.role));
}

function setAuthMode(mode) {
  state.authMode = mode;
  const registering = mode === "register";
  $("registerFields").classList.toggle("hidden", !registering);
  $("authTitle").textContent = registering ? "Create a workspace" : "Sign in";
  $("authDescription").textContent = registering
    ? "You get your own workspace. Projects, assets and credits stay inside it."
    : "You will only see the projects and assets inside your own workspace.";
  $("authSubmitBtn").textContent = registering ? "Create workspace" : "Sign in";
  $("authModeBtn").textContent = registering ? "Already have an account? Sign in" : "No account? Create one";
  $("authPassword").autocomplete = registering ? "new-password" : "current-password";
  $("authError").textContent = "";
}

async function submitAuth(event) {
  event.preventDefault();
  const registering = state.authMode === "register";
  const payload = {
    email: $("authEmail").value.trim(),
    password: $("authPassword").value,
    ...(registering ? {
      display_name: $("authDisplayName").value.trim(),
      workspace_name: $("authWorkspaceName").value.trim(),
    } : {}),
  };
  $("authSubmitBtn").disabled = true;
  $("authError").textContent = "";
  try {
    const result = await request(`/api/auth/${registering ? "register" : "login"}`, {
      method: "POST", body: JSON.stringify(payload),
    });
    unlockAuth(result.user);
    navigate("/app");
    await startWorkspace();
  } catch (error) {
    $("authError").textContent = error.message;
  } finally {
    $("authSubmitBtn").disabled = false;
  }
}

async function logout() {
  await request("/api/auth/logout", { method: "POST", body: "{}" }).catch(() => null);
  lockAuth();
  navigate("/");
}

let workspaceLoad = null;
async function startWorkspace() {
  if (workspaceLoad) return workspaceLoad;
  workspaceLoad = Promise.all([loadProjects(), loadPassengerModels(), loadImageTiers()])
    .finally(() => { workspaceLoad = null; });
  return workspaceLoad;
}

async function bootstrapAuth() {
  health();
  try {
    const user = await request("/api/auth/me");
    unlockAuth(user);
    if (!isAdminRoute()) await startWorkspace();
  } catch (_error) {
    // Not signed in. Which form shows is the route's call, not ours — landing
    // straight on /signup must not be reset to the sign-in copy.
    setAuth(null);
    setAuthMode(currentRoute() === "/signup" ? "register" : "login");
  }
}

async function health() {
  const pill = $("systemStatus");
  try {
    await request("/health");
    pill.className = "status-pill is-ok";
    pill.innerHTML = "<i></i>Online";
  } catch (_error) {
    pill.className = "status-pill is-danger";
    pill.innerHTML = "<i></i>Offline";
  }
}

async function loadCredits() {
  const workspace = (state.authUser?.workspaces || [])[0];
  if (!workspace) return;
  const billing = await request(`/v1/workspaces/${workspace.id}/billing`).catch(() => null);
  if (!billing) return;
  state.credits = billing.credit_balance;
  $("creditsAmount").textContent = `${Number(billing.credit_balance).toLocaleString()} credits`;
}

/* ============================================================
   Page switching
   ============================================================ */
const PAGE_HINT = {
  create: "Describe the frame, pick a quality level, generate.",
  "ai-director": "Bring a vague idea; approve the brief, visuals, bible and beats.",
  director: "Break a script into shots, then direct one shot at a time.",
  productions: "Everything you have made, with progress, cost and a way to recover a failure.",
  admin: "Provider gateway, skills, evidence and verified uploads.",
};

function switchPage(page) {
  state.page = page;
  document.querySelectorAll("[data-mode]").forEach((button) => {
    const active = button.dataset.mode === page;
    button.classList.toggle("active", active);
    // The nav is a set of page links, not tabs. aria-current has no "false"
    // that assistive tech treats as absent, so the inactive state is the
    // attribute being gone, not the string "false".
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-page]").forEach((node) => {
    node.hidden = node.dataset.page !== page;
  });
  $("appBody").classList.toggle("no-inspector", page === "admin");
  $("appActionBar").hidden = page === "admin" || page === "ai-director";
  if ($("modeDescription")) $("modeDescription").textContent = PAGE_HINT[page] || "";

  if (page === "admin" && state.authUser) loadOperations().catch((error) => toast(error.message));
  if (page === "productions") refreshProductions().catch((error) => toast(error.message));
  // The poll only paints the canvas while it is visible; a job that reached
  // its terminal state on another page is painted on the way back in.
  if (page === "create" && state.passengerJobs[state.passengerMedia]) {
    renderPassengerJob(state.passengerJobs[state.passengerMedia]).catch(() => null);
  }
  if (page === "ai-director" && state.project) {
    loadCreativeSessions().catch((error) => toast(error.message));
    syncCreativePolling();
  } else {
    stopCreativePolling();
  }
}

/* ============================================================
   Create page
   ============================================================ */
// What each medium's routes accept. Image ratios are the OpenRouter Images
// API's normalized set that Seedream also serves; video keeps the three the
// video routes are priced for.
const VIDEO_ASPECT_RATIOS = ["9:16", "16:9", "1:1"];
const IMAGE_ASPECT_RATIOS = ["1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16"];

function setAspectOptions(ratios) {
  const select = $("passengerAspect");
  const current = select.value;
  select.innerHTML = ratios.map((ratio) => `<option>${ratio}</option>`).join("");
  select.value = ratios.includes(current) ? current : ratios[0];
}

function setPassengerMedia(media) {
  state.passengerPrompts[state.passengerMedia] = $("passengerPrompt").value;
  state.passengerMedia = media;
  $("passengerPrompt").value = state.passengerPrompts[media];
  document.querySelectorAll("[data-media]").forEach((button) => {
    const active = button.dataset.media === media;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active)); // role="tab" needs the state, not just the class
  });
  const video = media === "video";
  // Image work picks a public quality level; only video exposes a route list.
  $("imageTaskGroup").hidden = video;
  $("imageModelGroup").hidden = video;
  $("videoModelGroup").hidden = !video;
  // Resolution is a video parameter. An image is priced per image and by its
  // quality level, and the images APIs refuse a video resolution; the ratio
  // list is the one each medium actually accepts.
  $("passengerResolutionField").classList.toggle("hidden", !video);
  setAspectOptions(video ? VIDEO_ASPECT_RATIOS : IMAGE_ASPECT_RATIOS);
  $("passengerDurationField").classList.toggle("hidden", !video);
  $("barDurationFact").hidden = !video;
  $("imagePromptActions").classList.toggle("hidden", video);
  $("passengerGenerateBtn").textContent = video ? "Generate video" : "Generate image";
  $("passengerPromptHeading").textContent = video ? "Describe the motion" : "Describe the frame";
  $("passengerReferenceLabel").textContent = video ? "First frame or character reference" : "Reference image";
  $("passengerPrompt").placeholder = video
    ? "e.g. Slow push toward the bottle, backlight sweeps the glass edge, the logo stays stable"
    : "e.g. Rainy convenience-store doorway at night, a girl turns with a lit phone, 35mm film, cold and warm light meeting";
  $("promptTypeBadge").textContent = video ? "Sent as written" : "Auto";
  $("promptCorrectionSummary").textContent = video
    ? "Video prompts are never rewritten by the image rules. What you wrote is what is submitted."
    : "Only composition, light, material and depth are enhanced. Your subject is never redesigned.";
  renderPassengerModels();
  const snapshot = state.passengerJobs[media];
  renderPassengerJob(snapshot);
  // Only one poll runs at a time, so switching media stops the other slot's
  // poll; coming back to a job that is still running resumes it rather than
  // leaving a frozen bar that promises a result nothing is fetching.
  if (snapshot && !TERMINAL_JOB_STATES.has(snapshot.status) && passengerPoll?.jobId !== snapshot.id) {
    startPassengerPolling(snapshot.id, media);
  }
  updatePassengerCost();
}

// The catalogues load unscoped at workspace start and again scoped to each
// selected project; the later request always wins, whichever response lands
// first, so a slow unscoped (most-restrictive) answer cannot paint FREE locks
// over a PRO project.
let modelCatalogueSequence = 0;
let imageTierSequence = 0;

async function loadPassengerModels() {
  // The full user-facing video catalogue, independent of provider credential
  // state: a route the platform sells does not vanish because a key is absent
  // — it renders disabled, and the server says why.
  // Plan locks are those of the active project's workspace — exactly what
  // admission applies; without a project the server under-promises.
  const sequence = ++modelCatalogueSequence;
  const scope = state.project ? `&project_id=${encodeURIComponent(state.project.id)}` : "";
  const catalogue = await request(`/v1/models?modality=video${scope}`);
  if (sequence !== modelCatalogueSequence) return; // superseded by a later load
  state.modelProfiles = catalogue.map((model) => ({ ...model, media: "video" }));
  renderPassengerModels();
}

async function loadImageTiers() {
  // Server truth for the three quality levels: what the plan allows and what
  // is actually runnable right now. The static array stays as fallback copy.
  const sequence = ++imageTierSequence;
  let tiers = null;
  try {
    const scope = state.project ? `?project_id=${encodeURIComponent(state.project.id)}` : "";
    const response = await request(`/v1/image-tiers${scope}`);
    tiers = Array.isArray(response) && response.length ? response : null;
  } catch (_error) {
    tiers = null;
  }
  if (sequence !== imageTierSequence) return; // superseded by a later load
  state.imageTiers = tiers;
  renderImageTierOptions();
}

function renderPassengerModels() {
  state.passengerModels = state.modelProfiles;
  // Server truth: the catalogue carries the active project's plan locks, so
  // a multi-workspace user on a PRO project is not told they are on FREE.
  const freeVideo = state.passengerMedia === "video"
    && state.modelProfiles.some((model) => model.plan_locked);
  const auto = '<option value="">Auto — BestShiny picks for you</option>';
  $("passengerModel").innerHTML = auto + state.modelProfiles.map((model) => {
    // Locked and unavailable routes stay visible: an option that disappears
    // is indistinguishable from one that never existed. The reason rides in
    // the visible label, not in a title: a title is never announced by a
    // screen reader on an <option> and does not exist at all on touch.
    const locked = Boolean(model.plan_locked);
    const unavailable = model.available === false;
    const suffix = locked ? " \u{1F512} Pro plan" : (unavailable ? " — unavailable right now" : "");
    return `<option value="${model.provider}|${model.model_id}"${locked || unavailable ? " disabled" : ""}>`
      + `${escapeHTML(friendlyModel(model.model_id))}${suffix}</option>`;
  }).join("");
  $("modelHint").textContent = freeVideo
    ? "Your Free plan runs video on Shiny Motion. Upgrade to Pro to unlock Shinier and Shiniest Motion."
    : "Auto lets BestShiny choose for this shot. Pick one yourself and exactly that one runs — if it cannot, the generation is refused rather than quietly swapped.";
  renderImageTierOptions();
  updatePassengerCost();
}

/** One view over the tier list: server truth when we have it, static fallback
 *  otherwise. Taglines are UI copy and always come from the static array. */
function imageTierViews() {
  const copy = Object.fromEntries(IMAGE_TIERS.map((tier) => [tier.value, tier]));
  if (state.imageTiers) {
    return state.imageTiers.map((remote) => ({
      value: remote.tier,
      stars: remote.stars || copy[remote.tier]?.stars || "✨",
      name: remote.name || copy[remote.tier]?.name || remote.tier,
      plan: remote.plan_requirement === "FREE" ? "Free" : "Pro",
      tagline: copy[remote.tier]?.tagline || "",
      locked: !remote.allowed_for_workspace,
      unavailable: !remote.available,
    }));
  }
  const free = isFreeWorkspace();
  return IMAGE_TIERS.map((tier) => ({
    ...tier,
    locked: free && tier.plan === "Pro",
    unavailable: false,
  }));
}

function renderImageTierOptions() {
  const select = $("passengerImageTier");
  if (!select) return;
  const previous = select.value;
  select.innerHTML = imageTierViews().map((tier) => {
    const disabled = tier.locked || tier.unavailable;
    const suffix = tier.locked
      ? " \u{1F512}"
      : (tier.unavailable ? " — unavailable right now" : "");
    const title = tier.unavailable ? "This quality level is temporarily unavailable." : "";
    return `<option value="${tier.value}"${disabled ? " disabled" : ""}${title ? ` title="${title}"` : ""}>`
      + `${tier.stars} ${escapeHTML(tier.name)}${suffix} — ${tier.plan}</option>`;
  }).join("");
  const keepable = [...select.options].find((option) => option.value === previous && !option.disabled);
  const firstEnabled = [...select.options].find((option) => !option.disabled);
  select.value = keepable ? previous : (firstEnabled ? firstEnabled.value : "shiny");
  syncImageTierHint();
}

function selectedImageTier() {
  const tiers = imageTierViews();
  return tiers.find((tier) => tier.value === $("passengerImageTier")?.value) || tiers[0];
}

function syncImageTierHint() {
  const tier = selectedImageTier();
  if (!tier) return;
  $("imageTierHint").textContent = tier.locked
    ? `${tier.name} is part of the Pro plan.`
    : (tier.unavailable
      ? `${tier.name} is temporarily unavailable. Pick another level — your prompt stays as written.`
      : tier.tagline);
}

function selectedPassengerModel() {
  const [provider, model] = $("passengerModel").value.split("|");
  return state.passengerModels.find((item) => item.provider === provider && item.model_id === model);
}

const isAutoModel = () => !$("passengerModel").value;

function passengerEstimatedCost() {
  // The catalogue deliberately carries no per-unit provider rates — nothing to
  // leak, nothing to drift. Every figure the user sees is the server's quote.
  return 0;
}

function updatePassengerCost() {
  const profile = state.passengerMedia === "image" ? null : selectedPassengerModel();
  $("barAspect").textContent = $("passengerAspect").value;
  $("barResolution").textContent = $("passengerResolution").value;
  $("barDuration").textContent = `${$("passengerDuration").value || 4}s`;
  // "None" was false: with no named profile the router still picks one.
  $("barModel").textContent = profile ? friendlyModel(profile.model_id) : "Auto";
  $("advModel").textContent = profile ? friendlyModel(profile.model_id) : "—";
  $("advPricing").textContent = "Quoted before you generate";
  syncImageTierHint();

  if (!profile) {
    // Nothing is quoted until the platform has resolved a target, so the figure
    // the user sees can never belong to a model other than the one that runs.
    const routed = state.passengerMedia === "image" || isAutoModel();
    $("passengerCost").textContent = routed ? "Quoted on submit" : "Pick a quality level";
    if (state.passengerMedia === "image") {
      const tier = selectedImageTier();
      $("barModel").textContent = `${tier.stars} ${tier.name}`;
      $("advModel").textContent = `${tier.stars} ${tier.name}`;
    }
    // Video with no resolved profile keeps the "Auto" set above: naming a
    // model the router has not picked yet would be a promise we cannot keep.
    return;
  }
  $("passengerCost").textContent = "Quoted on submit";
}

function passengerReferenceFingerprint(projectId, file) {
  return JSON.stringify([projectId, file.name, file.size, file.lastModified, file.type]);
}

async function uploadPassengerReference({ projectId, file }) {
  if (!file) return null;
  if (!projectId) throw new Error("Create a project first");
  const fingerprint = passengerReferenceFingerprint(projectId, file);
  const cached = state.passengerReferenceUpload;
  if (cached?.fingerprint === fingerprint && cached.file === file) {
    if (cached.assetId) return cached.assetId;
    if (cached.promise) return cached.promise;
  }
  const form = new FormData();
  form.append("project_id", projectId);
  form.append("asset_type", "REFERENCE");
  form.append("file", file);
  const upload = (async () => {
    const response = await fetch(`${API}/v1/assets`, {
      method: "POST", body: form, credentials: "include", headers: csrfHeaders("POST"),
    });
    if (!response.ok) throw new Error("Reference upload failed");
    const asset = await response.json();
    if (state.passengerReferenceUpload?.fingerprint === fingerprint
      && state.passengerReferenceUpload.file === file) {
      state.passengerReferenceUpload = { fingerprint, file, assetId: asset.id, promise: null };
    }
    return asset.id;
  })();
  state.passengerReferenceUpload = { fingerprint, file, assetId: null, promise: upload };
  try {
    return await upload;
  } catch (error) {
    if (state.passengerReferenceUpload?.promise === upload) state.passengerReferenceUpload = null;
    throw error;
  }
}

async function correctPassengerPrompt() {
  if (state.passengerMedia !== "image") return toast("Prompt improvement is for images. Video is submitted as written.");
  const prompt = $("passengerPrompt").value.trim();
  if (!prompt) return toast("Describe the frame first");
  const projectId = state.project?.id || null;
  const reference = await uploadPassengerReference({ projectId, file: $("passengerReference").files[0] });
  const path = projectId ? `/api/prompt/correct?project_id=${projectId}` : "/api/prompt/correct";
  const result = await request(path, {
    method: "POST",
    body: JSON.stringify({ prompt, task_type: "auto", reference_assets: reference ? [reference] : [] }),
  });
  state.passengerOriginal ??= result.original_prompt;
  $("passengerPrompt").value = result.corrected_prompt;
  $("undoImagePromptBtn").disabled = false;
  $("promptTypeBadge").textContent = result.identity_preservation_mode ? "Identity preserving" : simpleLabel(result.detected_type);
  $("promptCorrectionSummary").textContent = `${result.changes.length} details enhanced, ${result.preserved_constraints.length} of your constraints kept.`;
  toast("Prompt improved. Edit it further or undo.");
}

async function refinePassengerPrompt() {
  if (!state.project) return toast("Create a project first");
  const prompt = $("passengerPrompt").value.trim();
  if (!prompt) return toast("Describe the frame first");
  const button = $("refinePromptBtn");
  button.disabled = true;
  button.textContent = "Refining…";
  try {
    const result = await request("/v1/prompts/refine", {
      method: "POST",
      body: JSON.stringify({ project_id: state.project.id, prompt }),
    });
    state.passengerOriginal ??= result.original || prompt;
    $("passengerPrompt").value = result.refined;
    $("undoImagePromptBtn").disabled = false;
    $("promptTypeBadge").textContent = "Refined";
    $("promptCorrectionSummary").textContent = `Deep refine complete. ${result.preserved_facts?.length || 0} facts locked.`;
    toast("Prompt refined. Your original facts stay locked.");
  } finally {
    button.disabled = false;
    button.textContent = "Deep refine";
  }
}

function undoPassengerPrompt() {
  if (!state.passengerOriginal) return;
  $("passengerPrompt").value = state.passengerOriginal;
  state.passengerOriginal = null;
  $("undoImagePromptBtn").disabled = true;
  $("promptTypeBadge").textContent = "Restored";
  $("promptCorrectionSummary").textContent = "Your original prompt is back.";
}

/* ---- Empty-state furniture -------------------------------------
   Line art at 26px inside the 56px well, not a text glyph: the old set
   (▣ ◷ ◻ ▦ ✦ ◇) reused ◷ for BOTH "generating" and "no jobs", so the same
   mark meant two opposite things on the same page. Every empty state also
   ends in exactly ONE .btn-primary; every other exit is .btn-tertiary. */
const ICON_FRAME = `<svg viewBox="0 0 32 32" fill="none" aria-hidden="true">
  <rect x="2.5" y="6.5" width="27" height="19" rx="3" stroke="currentColor" stroke-width="1.6"/>
  <path d="M8.6 20.2l4.6-5.4a1.4 1.4 0 0 1 2.1 0l3 3.6 2-2.2a1.4 1.4 0 0 1 2.1.05l3 3.9"
        stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" opacity=".6"/>
  <circle cx="11.3" cy="12.3" r="1.8" stroke="currentColor" stroke-width="1.6"/>
  <path d="M12 29.4v-1.8M16 29.4v-1.8M20 29.4v-1.8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" opacity=".45"/>
</svg>`;
const ICON_PROJECT = `<svg viewBox="0 0 32 32" fill="none" aria-hidden="true">
  <path d="M3.5 9.2A2.7 2.7 0 0 1 6.2 6.5h5.3l2.6 3.1h11.7a2.7 2.7 0 0 1 2.7 2.7v11.5a2.7 2.7 0 0 1-2.7 2.7H6.2a2.7 2.7 0 0 1-2.7-2.7V9.2Z"
        stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
  <path d="M16 15.4v6.6M12.7 18.7h6.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
</svg>`;
const ICON_ALERT = `<svg viewBox="0 0 32 32" fill="none" aria-hidden="true">
  <path d="M16 4.8 29 27.2H3L16 4.8Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
  <path d="M16 13v6.4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
  <circle cx="16" cy="23" r="1.2" fill="currentColor"/></svg>`;

/* One literal for the character rail's empty state, because the reset path and
   the render path both paint it and a drifting pair reads as a flicker. */
const CHARACTERS_EMPTY = `
  <div class="empty-block is-compact">
    <strong>No characters locked yet</strong>
    <p>Add one so every later shot can hold the same face.</p>
    <div class="btn-row btn-row-center">
      <button class="btn btn-tertiary" type="button">Add a character</button>
    </div>
  </div>`;

function bindCharactersEmptyCta(list) {
  // #createCharacterBtn validates the name field first, so with an empty name
  // it would only bounce off its own guard: send the user to the field.
  list.querySelector(".empty-block button")?.addEventListener("click", () => {
    const name = $("characterName");
    const panel = name?.closest("details");
    if (panel && !panel.open) panel.open = true;
    if (name && !name.value.trim()) { name.focus(); return; }
    $("createCharacterBtn").click();
  });
}

/* An empty state that cannot be acted on is just a label. Both branches here
   end in a button that moves the user forward. */
function emptyCanvasMarkup() {
  if (!state.project) {
    return `
      <div class="empty-block">
        <span class="empty-icon" aria-hidden="true">${ICON_PROJECT}</span>
        <strong>Open a project to start shooting</strong>
        <p>Frames, shots, characters and credits all live inside a project. Make one and this canvas comes alive.</p>
        <div class="btn-row btn-row-center">
          <button class="btn btn-primary" type="button" data-empty-action="new-project">Create a project</button>
        </div>
      </div>`;
  }
  // The example pill seeds the prompt box with its own placeholder, so the
  // suggestion is always the one the field itself is offering for this medium.
  const example = ($("passengerPrompt")?.placeholder || "").replace(/^e\.g\.\s*/, "");
  return `
    <div class="empty-block">
      <span class="empty-icon" aria-hidden="true">${ICON_FRAME}</span>
      <strong>Describe the frame you want</strong>
      <p>Write it on the left the way you'd tell a DP — subject, light, lens, mood. Or drop a reference image anywhere on this canvas.</p>
      <div class="btn-row btn-row-center">
        <button class="btn btn-primary" type="button" data-empty-action="generate">${state.passengerMedia === "video" ? "Generate video" : "Generate image"}</button>
        <button class="btn btn-tertiary" type="button" data-empty-action="upload">Use a reference image</button>
      </div>
      ${example ? `<button class="btn empty-example" type="button" data-empty-action="example">${escapeHTML(example)}</button>` : ""}
    </div>`;
}

/* ---- Create-canvas progress ------------------------------------
   Real provider progress when the gateway has polled one; otherwise a
   stage-based estimate from the job's event trail. The displayed value
   never moves backwards, and with no signal at all the bar shimmers. */
const TERMINAL_JOB_STATES = new Set(["COMPLETED", "FAILED", "CANCELLED", "WORKER_NEEDS_USER_ACTION"]);
const STAGE_PROGRESS = {
  REQUEST_SUBMITTED: 10, WORKER_SELECTED: 20,
  MEDIA_DOWNLOADED: 90, VIDEO_GENERATED: 90,
  JOB_COMPLETED: 100,
};
const PASSENGER_POLL_INTERVAL_MS = 2500;
const PASSENGER_POLL_BUDGET_MS = 10 * 60 * 1000;
let passengerPoll = null;

function stopPassengerPolling() {
  if (!passengerPoll) return;
  window.clearTimeout(passengerPoll.timer);
  passengerPoll = null;
}

function estimateJobProgress(job, startedAt) {
  if (job.status === "COMPLETED") return 100;
  const events = job.events || [];
  let provider = null;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.type === "PROVIDER_JOB_POLL" && typeof event.detail?.progress === "number") {
      provider = Math.max(0, Math.min(1, event.detail.progress)) * 100;
      break;
    }
  }
  let stage = null;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const mapped = STAGE_PROGRESS[events[index].type];
    if (mapped !== undefined) { stage = mapped; break; }
  }
  let estimate = stage;
  if (["RUNNING", "GENERATING", "SUBMITTED"].includes(job.status)) {
    // Creep with elapsed time toward 80%, never past it.
    const minutes = (Date.now() - startedAt) / 60000;
    estimate = Math.max(stage ?? 0, Math.min(80, 20 + minutes * 12));
  }
  // A provider that reports real progress (OpenRouter) moves the bar past
  // the creep. Most report a placeholder — 0.0 while queued, 0.5 while
  // running from Seedance, Wan, RunAPI and Flow — which must not freeze the
  // bar below the creep for the whole generation.
  if (provider === null) return estimate;               // no signal yet -> shimmer
  // A provider reporting 0.0 is saying "queued", not "0% done": taken as a
  // number it pins a determinate bar to zero for the whole wait.
  if (provider === 0 && estimate === null) return null;  // "queued" is not progress
  return estimate === null ? provider : Math.max(provider, estimate);
}

/** Monotonic progress for the job being polled: 0-100, or null meaning
 *  "no signal — render indeterminate".
 *  `shown` starts as null, not 0: `??` treats 0 as a value, so seeding it
 *  with 0 made this return 0 forever and the indeterminate branch was
 *  unreachable dead code. */
function passengerDisplayProgress(job) {
  const poll = passengerPoll && passengerPoll.jobId === job.id ? passengerPoll : null;
  const raw = estimateJobProgress(job, poll?.startedAt ?? Date.now());
  if (raw === null) return poll?.shown ?? null;      // stays null until a real number arrives
  const shown = Math.max(poll?.shown ?? 0, Math.min(100, Math.round(raw)));
  if (poll) poll.shown = shown;
  return shown;
}

/* ---- The generating block ---------------------------------------
   Status-driven copy, an elapsed clock that ticks between polls, and a bar
   that is honestly indeterminate when nothing has told us a number. */
const GENERATING_COPY = {
  RESERVED:   { title: "Reserving your credits",   note: "Nothing is charged until a model accepts the job." },
  NEW:        { title: "Choosing a model",         note: "Matching this frame to the model that shoots it best." },
  QUEUED:     { title: "Waiting in the queue",     note: "The model is busy right now. Your place is held." },
  SUBMITTED:  { title: "The model has your frame", note: "First pixels usually arrive inside a minute." },
  RUNNING:    { title: "Rendering your frame",     note: "" },
  GENERATING: { title: "Rendering your frame",     note: "" },
  RETRY_WAIT: { title: "Trying again",             note: "The first attempt didn't return. You are only charged once." },
};
const clockOf = (ms) => `${Math.floor(ms / 60000)}:${String(Math.floor((ms % 60000) / 1000)).padStart(2, "0")}`;
const genNote = (job, det) => GENERATING_COPY[job.status]?.note
  || (det ? "The frame lands here the moment it's ready."
          : "This model doesn't report progress — the timer is the honest signal.");

function generatingMarkup(job, { progress, startedAt }) {
  const det = typeof progress === "number";
  const pct = det ? Math.round(progress) : null;
  // An ABSENT aria-valuenow is the correct ARIA encoding of an indeterminate
  // bar. Never emit a fake number: it would be announced as real progress.
  return `
  <div class="canvas-generating" data-progress-stage data-job-id="${escapeHTML(job.id)}"
       data-started="${startedAt}" aria-busy="true">
    <div class="gen-bloom" aria-hidden="true"></div>
    <div class="gen-gate" aria-hidden="true"><i></i><i></i><i></i></div>
    <p class="gen-title" role="status">${escapeHTML(GENERATING_COPY[job.status]?.title || "Rendering your frame")}</p>
    <div class="gen-track ${det ? "is-determinate" : "is-indeterminate"}"
         role="progressbar" aria-label="Generation progress"
         aria-valuemin="0" aria-valuemax="100"${det ? ` aria-valuenow="${pct}"` : ""}>
      <i class="gen-fill"${det ? ` style="width:${pct}%"` : ""}></i>
    </div>
    <p class="gen-sub mono">
      <span data-gen-pct>${det ? `${pct}%` : "Working"}</span> ·
      <span data-gen-clock>${clockOf(Date.now() - startedAt)}</span> ·
      ${escapeHTML(friendlyModel(job.model))}
    </p>
    <p class="gen-note" data-gen-note>${escapeHTML(genNote(job, det))}</p>
    <button class="btn btn-tertiary" type="button" data-gen-cancel="${escapeHTML(job.id)}">Cancel</button>
  </div>`;
}

/** A failed take is a rim and a sentence, never a red canvas: a red fill
 *  reads as a system alarm rather than "this one didn't come back". */
function failedMarkup(job) {
  const cancelled = job.status === "CANCELLED";
  return `
  <div class="canvas-failed">
    <span class="empty-icon" aria-hidden="true">${ICON_ALERT}</span>
    <strong>${cancelled ? "You cancelled this one" : "This one didn't come back"}</strong>
    <p>${cancelled
      ? "Nothing was produced. Your balance updates once the reserved credits are released."
      : "This generation failed. Nothing usable was produced, and the credits it reserved are released once the job settles."}</p>
    ${job.error_message ? `<details class="gen-details"><summary>What the model said</summary><p>${escapeHTML(job.error_message)}</p></details>` : ""}
    <div class="btn-row btn-row-center">
      <button class="btn btn-primary" type="button" data-empty-action="generate">Try another take</button>
    </div>
  </div>`;
}

/** Ten minutes of polling is our budget, not the job's. Saying "failed" here
 *  would be a lie: the job is alive, we simply stopped watching. */
function stalledMarkup(job) {
  return `
  <div class="canvas-generating is-stalled" data-job-id="${escapeHTML(job.id)}">
    <div class="gen-bloom" aria-hidden="true"></div>
    <div class="gen-gate" aria-hidden="true"><i></i><i></i><i></i></div>
    <p class="gen-title" role="status">Still running — we stopped watching</p>
    <p class="gen-note">This has been going for ten minutes, so BestShiny stopped checking on it. Your credits stay reserved until the job settles.</p>
    <div class="btn-row btn-row-center">
      <button class="btn btn-primary" type="button" data-gen-resume="${escapeHTML(job.id)}">Check again</button>
      <button class="btn btn-tertiary" type="button" data-gen-cancel="${escapeHTML(job.id)}">Cancel</button>
    </div>
  </div>`;
}

/* The clock must tick BETWEEN the 2.5s polls, or the only honest signal in
   the indeterminate state moves once every three seconds. */
let genClock = null;
function startGenClock(stage) {
  window.clearInterval(genClock);
  const block = stage.querySelector("[data-progress-stage]");
  const node = stage.querySelector("[data-gen-clock]");
  if (!block || !node) return;
  const started = Number(block.dataset.started) || Date.now();
  genClock = window.setInterval(() => {
    if (!node.isConnected) return window.clearInterval(genClock);
    node.textContent = clockOf(Date.now() - started);
  }, 1000);
}

/** Repaint the canvas without losing the drag-over overlay. The overlay is
 *  static markup inside #passengerResult, so an innerHTML rewrite deletes it
 *  and the whole-canvas drop target would work exactly once. */
function paintStage(stage, html) {
  const overlay = stage.querySelector(".canvas-drop");
  stage.innerHTML = html;
  if (overlay) stage.append(overlay);
}

/** Speak one sentence into the canvas live region. The node is permanent and
 *  lives outside the stage, so a screen reader hears the change; writing the
 *  same text twice is a no-op rather than a repeated announcement. */
function announceCanvas(message) {
  const node = $("canvasAnnounce");
  if (node && node.textContent !== message) node.textContent = message;
}

/** The ratio, resolution and duration we actually asked for, plus the
 *  timeout flag, live on the job object: the server is not guaranteed to
 *  echo the request back, and every poll tick replaces the job wholesale. */
function carryPassengerJobLocals(next, previous) {
  if (next && previous?.__req && !next.__req) next.__req = previous.__req;
  return next;
}

function startPassengerPolling(jobId, media) {
  stopPassengerPolling(); // only ever one active poll per page
  // shown starts as null, not 0 — see passengerDisplayProgress.
  const poll = { jobId, media, startedAt: Date.now(), shown: null, timedOut: false, timer: null };
  passengerPoll = poll;
  const tick = async () => {
    if (passengerPoll !== poll) return;
    const job = await request(`/v1/generations/${encodeURIComponent(jobId)}`).catch(() => null);
    if (passengerPoll !== poll) return;
    if (job) {
      carryPassengerJobLocals(job, state.passengerJobs[media]);
      rememberJob({ ...job, progress: passengerDisplayProgress(job) });
      state.passengerJobs[media] = job;
      if (state.passengerMedia === media && state.page === "create") {
        await renderPassengerJob(job).catch(() => null);
      }
      if (state.page === "productions") renderProductions();
      if (TERMINAL_JOB_STATES.has(job.status)) {
        stopPassengerPolling();
        await loadCredits().catch(() => null);
        if (job.status === "COMPLETED") toast("Your creation is ready.");
        else if (job.status === "FAILED") toast(job.error_message || "This generation failed. Nothing usable was produced.");
        return;
      }
    }
    if (Date.now() - poll.startedAt > PASSENGER_POLL_BUDGET_MS) {
      poll.timedOut = true;
      if (job) {
        // The flag belongs to the JOB, not the poll: stopPassengerPolling()
        // nulls passengerPoll a line below, and a media toggle or page switch
        // would then silently revert the honest "we stopped watching" copy.
        job.__timedOut = true;
        state.passengerJobs[media] = job;
      }
      if (job && state.passengerMedia === media && state.page === "create") {
        await renderPassengerJob(job).catch(() => null);
      }
      stopPassengerPolling();
      return;
    }
    poll.timer = window.setTimeout(() => { tick(); }, PASSENGER_POLL_INTERVAL_MS);
  };
  poll.timer = window.setTimeout(() => { tick(); }, PASSENGER_POLL_INTERVAL_MS);
}

async function renderPassengerJob(job) {
  const stage = $("passengerResult");
  if (!job) {
    if (state.passengerPreviewObjectUrl) URL.revokeObjectURL(state.passengerPreviewObjectUrl);
    state.passengerPreviewObjectUrl = null;
    stage.className = "canvas-stage empty-state";
    paintStage(stage, emptyCanvasMarkup());
    $("saveToProjectBtn").disabled = true;
    $("promotePassengerAssetBtn").disabled = true;
    $("promotePassengerAssetBtn").textContent = "Save version";
    return;
  }
  rememberJob(job);
  $("operationsJobId").value = job.id;

  const reconciling = job.credit_status === "RECONCILIATION_REQUIRED";
  const displayedStatus = reconciling ? "Checking the charge · credits still held" : simpleLabel(job.status);
  const tone = reconciling ? "is-queued" : statusTone(job.status);
  const running = !TERMINAL_JOB_STATES.has(job.status);
  const progress = running ? passengerDisplayProgress(job) : 100;
  // The flag lives on the job so it survives stopPassengerPolling() nulling
  // passengerPoll; the poll object is still consulted for the tick that sets it.
  const timedOut = Boolean(job.__timedOut || (passengerPoll?.jobId === job.id && passengerPoll.timedOut));

  if (running && !job.output_asset_id && !timedOut) {
    // In-place update keeps the width transition animating instead of
    // rebuilding the bar at its new width every poll tick. The timeout tick
    // must fall through to the full render so its copy actually appears.
    // Every status-driven string is refreshed here too: updating only the bar
    // froze the headline and the note at whatever they said on first render,
    // for the whole generation.
    const existing = stage.querySelector("[data-progress-stage]");
    if (existing && existing.dataset.jobId === job.id) {
      const track = existing.querySelector(".gen-track");
      const fill = existing.querySelector(".gen-fill");
      const det = typeof progress === "number";
      track.classList.toggle("is-determinate", det);
      track.classList.toggle("is-indeterminate", !det);
      if (det) { fill.style.width = `${progress}%`; track.setAttribute("aria-valuenow", String(progress)); }
      else { fill.style.removeProperty("width"); track.removeAttribute("aria-valuenow"); }
      existing.querySelector("[data-gen-pct]").textContent = det ? `${progress}%` : "Working";
      // .gen-title is a live region. Writing it unconditionally re-announces the
      // same sentence on every poll tick, so only write it when it actually changes.
      const title = existing.querySelector(".gen-title");
      const nextTitle = GENERATING_COPY[job.status]?.title || "Rendering your frame";
      if (title.textContent !== nextTitle) title.textContent = nextTitle;
      existing.querySelector("[data-gen-note]").textContent = genNote(job, det);
      const chip = stage.querySelector(".result-bar .status-chip");
      if (chip) { chip.className = `status-chip ${tone}`; chip.textContent = displayedStatus; }
      return;
    }
  }

  if (state.passengerPreviewObjectUrl) URL.revokeObjectURL(state.passengerPreviewObjectUrl);
  state.passengerPreviewObjectUrl = null;

  let preview = "";
  if (job.output_asset_id) {
    const asset = await request(`/v1/assets/${job.output_asset_id}`).catch(() => null);
    let mediaUrl = asset?.public_url || "";
    const isProtectedLocalMedia = asset?.storage_key && (() => {
      try { return new URL(asset.public_url, location.href).pathname.includes("/v1/storage/"); }
      catch (_error) { return false; }
    })();
    if (isProtectedLocalMedia) {
      const storagePath = asset.storage_key.split("/").map(encodeURIComponent).join("/");
      const response = await fetch(`${API}/v1/storage/${storagePath}`, { credentials: "include" });
      if (response.ok) {
        state.passengerPreviewObjectUrl = URL.createObjectURL(await response.blob());
        mediaUrl = state.passengerPreviewObjectUrl;
      }
    }
    if (mediaUrl && asset.mime_type?.startsWith("image/")) {
      preview = `<img class="result-preview fade-in" src="${escapeHTML(mediaUrl)}" alt="Generated result" />`;
    } else if (mediaUrl && asset.mime_type?.startsWith("video/")) {
      preview = `<video class="result-preview fade-in" src="${escapeHTML(mediaUrl)}" controls playsinline></video>`;
    }
  }

  // The request's own facts, because the server is not guaranteed to echo
  // them back on the job view.
  const asked = job.__req || {};
  const isVideo = (job.generation_type || job.media_type || state.passengerMedia) === "video";
  const frame = [job.aspect_ratio || asked.aspect_ratio, job.resolution || asked.resolution]
    .filter(Boolean).join(" · ") || "—";
  const length = job.duration ?? asked.duration ?? null;
  const credits = jobCredits(job);
  const resultBar = `
    <div class="result-bar">
      <span class="status-chip ${tone}">${escapeHTML(displayedStatus)}</span>
      <button class="result-id" type="button" data-copy-id="${escapeHTML(job.id)}"
              title="Copy this creation's ID">Copy ID</button>
      <div class="result-meta">
        <div><span>Look</span><strong>${escapeHTML(friendlyModel(job.model))}</strong></div>
        <div><span>Frame</span><strong>${escapeHTML(frame)}</strong></div>
        ${isVideo && length ? `<div><span>Length</span><strong>${escapeHTML(String(length))}s</strong></div>` : ""}
        <div class="is-cost"><span>Cost</span><strong>${credits ? `${credits} credits` : "—"}</strong></div>
      </div>
    </div>`;

  // has-result fires ONLY when there is a result. It flips the stage from
  // flex to block, so applying it to the waiting/failed/stalled blocks was
  // what jammed them against the top of a 60vh dark rectangle.
  const failed = ["FAILED", "CANCELLED"].includes(job.status);
  const stateCls = failed ? "is-failed"
    : timedOut ? "is-stalled"
      : preview ? "has-result"
        : "is-generating";
  stage.className = `canvas-stage ${stateCls}`;
  const body = failed ? failedMarkup(job)
    : timedOut ? stalledMarkup(job)
      : preview || generatingMarkup(job, { progress, startedAt: passengerPoll?.startedAt ?? Date.now() });
  paintStage(stage, `${body}${preview ? resultBar : ""}`);
  // #passengerResult carries no aria-live: its whole subtree is rewritten on every
  // render, which would re-announce the entire result bar each tick. #canvasAnnounce
  // is a permanent node outside the stage, so setting its text is actually heard.
  announceCanvas(preview && job.status === "COMPLETED"
    ? `Your ${isVideo ? "video" : "frame"} is ready.`
    : job.status === "FAILED" ? "The generation failed." : "");
  if (stateCls === "is-generating") startGenClock(stage);
  const confirmed = state.confirmedAssets.has(job.output_asset_id);
  $("saveToProjectBtn").disabled = !job.output_asset_id || confirmed;
  $("saveToProjectBtn").textContent = confirmed ? "Saved to project" : "Save to project";
  $("promotePassengerAssetBtn").disabled = !job.output_asset_id || confirmed;
}

async function generatePassenger() {
  if (!state.project) return toast("Create a project first");
  const prompt = $("passengerPrompt").value.trim();
  const isImage = state.passengerMedia === "image";
  const selection = isImage ? null : selectedPassengerModel();
  const auto = isImage || isAutoModel();
  const imageTask = $("passengerImageTask").value;
  if (!prompt) return toast("Write a prompt first");
  if (!isImage && !auto && !selection) return toast("Pick a model, or choose Auto");
  const projectId = state.project.id;
  const mediaType = state.passengerMedia;
  const aspectRatio = $("passengerAspect").value;
  const resolution = $("passengerResolution").value;
  const duration = mediaType === "video" ? Number($("passengerDuration").value || 4) : null;
  const negativePrompt = $("passengerNegativePrompt").value.trim();
  const criticality = $("passengerCriticality").value;
  const imageTier = isImage ? $("passengerImageTier").value : null;
  const estimatedCost = passengerEstimatedCost();
  const freeVideo = mediaType === "video"
    && state.authUser?.workspaces?.some((workspace) => workspace.plan_tier === "FREE");
  const file = $("passengerReference").files[0];
  const fingerprint = JSON.stringify({
    projectId, mediaType,
    provider: auto ? "" : selection.provider,
    model: auto ? "" : selection.model_id,
    imageTask: isImage ? imageTask : null,
    imageTier,
    modelRole: !isImage && auto && freeVideo ? "VIDEO_SEEDANCE" : null,
    prompt, negativePrompt, criticality, aspectRatio, resolution, duration, estimatedCost,
    file: file ? [file.name, file.size, file.lastModified] : null,
  });
  const idempotencyKey = beginSubmission("passenger", fingerprint);
  if (!idempotencyKey) return;
  const button = $("passengerGenerateBtn");
  button.disabled = true;
  button.textContent = "Submitting…";
  ["passengerPrompt", "passengerModel", "passengerImageTask", "passengerAspect",
    "passengerResolution", "passengerDuration", "passengerReference"]
    .forEach((id) => { const node = $(id); if (node) node.disabled = true; });
  let succeeded = false;
  try {
    const reference = await uploadPassengerReference({ projectId, file });
    const payload = {
      project_id: projectId,
      media_type: mediaType,
      // Images never carry a model: the creative task is the whole request and
      // the router resolves the target. Video sends a named model alone, since
      // pairing it with a role would ask the server to route and obey at once.
      provider: auto ? "" : selection.provider,
      model: auto ? "" : selection.model_id,
      ...(isImage ? { image_task: imageTask, image_tier: imageTier } : {}),
      ...(!isImage && auto && freeVideo ? { model_role: "VIDEO_SEEDANCE" } : {}),
      prompt,
      ...(negativePrompt ? { negative_prompt: negativePrompt } : {}),
      asset_criticality: criticality,
      aspect_ratio: aspectRatio,
      // Resolution travels only with video; an image job carries none.
      ...(isImage ? {} : { resolution }),
      reference_asset_ids: reference ? [reference] : [],
      idempotency_key: idempotencyKey,
      estimated_cost: estimatedCost,
    };
    if (duration !== null) payload.duration = duration;
    const job = await request("/api/passenger/generate", { method: "POST", body: JSON.stringify(payload) });
    // What we asked for, kept on the job: the result bar reports the frame and
    // the length, and the job view is not guaranteed to echo them back.
    job.__req = { aspect_ratio: aspectRatio, resolution: isImage ? "" : resolution, duration };
    state.passengerJobs[mediaType] = job;
    await renderPassengerJob(job);
    startPassengerPolling(job.id, mediaType);
    await loadCredits();
    succeeded = true;
    toast(auto
      ? `Submitted on ${friendlyModel(job.model)} — ${job.estimated_credits} credits reserved.`
      : "Submitted. The model you chose is the one that runs and the one you are billed for.");
  } finally {
    finishSubmission("passenger", idempotencyKey, succeeded);
    button.disabled = false;
    ["passengerPrompt", "passengerModel", "passengerImageTask", "passengerAspect",
      "passengerResolution", "passengerDuration", "passengerReference"]
      .forEach((id) => { const node = $(id); if (node) node.disabled = false; });
    button.textContent = state.passengerMedia === "video" ? "Generate video" : "Generate image";
  }
}

async function refreshPassengerJob() {
  const current = state.passengerJobs[state.passengerMedia];
  if (!current) return toast("No generation running");
  const job = await request(`/v1/generations/${current.id}`);
  // The request facts carry over; __timedOut deliberately does not — asking
  // for a refresh IS the user saying "start watching again".
  carryPassengerJobLocals(job, current);
  state.passengerJobs[state.passengerMedia] = job;
  await renderPassengerJob(job);
  // A manual refresh on a live job also restarts the poll loop (e.g. after
  // the ten-minute budget gave up).
  if (!TERMINAL_JOB_STATES.has(job.status) && passengerPoll?.jobId !== job.id) {
    startPassengerPolling(job.id, state.passengerMedia);
  }
  // A job that fails before submission has its reservation refunded server-side.
  // Without this the pill keeps showing the reserved balance and the credits look
  // lost, which is exactly the moment a user is most likely to distrust the meter.
  await loadCredits();
}

async function confirmPassengerAsset() {
  // The dialog acts on the gallery job it was opened for, or falls back to
  // the Create canvas's current job.
  const job = (state.savingJobId && state.jobs.get(state.savingJobId))
    || state.passengerJobs[state.passengerMedia];
  if (!job?.output_asset_id) return toast("Wait for the generation to finish");
  const kind = job.generation_type || state.passengerMedia;
  const name = $("passengerAssetName").value.trim() || `${kind === "video" ? "Video" : "Image"} asset`;
  const result = await request(`/api/generations/${job.id}/promote`, {
    method: "POST",
    body: JSON.stringify({
      asset_id: $("passengerExistingAsset").value || null,
      asset_type: $("passengerAssetType").value,
      name,
      promote_to_canonical: $("passengerPromoteCanonical").checked,
      reason: $("passengerPromoteCanonical").checked ? "Explicitly set as canonical from the Create canvas" : "",
    }),
  });
  state.confirmedAssets.add(job.output_asset_id);
  state.savingJobId = null;
  await loadLogicalAssets();
  if (job.id === state.passengerJobs[state.passengerMedia]?.id) await renderPassengerJob(job);
  renderProductions();
  $("saveAssetDialog").close();
  toast(result.canonical ? "Version saved and set as the main reference" : "Version saved to the project");
}

/* ============================================================
   Projects and assets
   ============================================================ */
async function loadProjects() {
  state.projects = await request("/v1/projects");
  $("projectSelect").innerHTML = state.projects.length
    ? state.projects.map((project) => `<option value="${project.id}">${escapeHTML(project.name)}</option>`).join("")
    : '<option value="">No projects yet</option>';
  if (state.projects.length) await selectProject(state.projects[0].id);
  else clearWorkspaceState();
}

async function loadLogicalAssets() {
  if (!state.project) return;
  [state.logicalAssets, state.styleLock] = await Promise.all([
    request(`/api/projects/${state.project.id}/assets`),
    request(`/api/projects/${state.project.id}/style-lock`),
  ]);
  const options = (canonicalLabel) => '<option value="">Create a new asset</option>' + state.logicalAssets
    .map((asset) => `<option value="${asset.id}">${simpleLabel(asset.asset_type)} · ${escapeHTML(asset.name)}${asset.canonical_version_id ? canonicalLabel : ""}</option>`)
    .join("");
  $("passengerExistingAsset").innerHTML = options(" · main reference");
  $("manualExistingAsset").innerHTML = options(" · main reference");
  renderAssetRail();
  renderProjectStyleLock();
}

function renderAssetRail() {
  const rail = $("assetRail");
  if (!rail) return;
  if (!state.logicalAssets.length) {
    rail.classList.add("empty");
    rail.innerHTML = `
      <div class="empty-block is-compact">
        <strong>No project assets yet</strong>
        <p>Save a finished frame and it becomes a reference every later shot can reuse.</p>
        <div class="btn-row btn-row-center">
          <button class="btn btn-tertiary" type="button" data-empty-action="go-create">Save a frame</button>
        </div>
      </div>`;
    return;
  }
  rail.classList.remove("empty");
  rail.innerHTML = state.logicalAssets.map((asset) => `
    <button class="asset-chip" type="button" data-asset="${escapeHTML(asset.id)}">
      <span class="asset-chip-thumb" data-rail-thumb="${escapeHTML(asset.id)}" aria-hidden="true">▣</span>
      <span class="asset-kind" style="color:${assetKindColor(asset.asset_type)}">${escapeHTML(simpleLabel(asset.asset_type)).toUpperCase()}</span>
      <span class="asset-name">${escapeHTML(asset.name)}</span>
      ${asset.canonical_version_id ? '<span class="asset-flag">●</span>' : ""}
    </button>`).join("");
  rail.querySelectorAll("[data-asset]").forEach((button) => {
    button.addEventListener("click", () => openAssetDetails(button.dataset.asset));
  });
  hydrateAssetRailThumbs();
}

/** The media asset behind an asset's canonical version, cached per version so
 *  a promotion (new canonical id) refreshes and everything else does not. */
async function assetCanonicalMediaId(asset) {
  if (!asset?.canonical_version_id) return null;
  if (state.assetMediaIds.has(asset.canonical_version_id)) {
    return state.assetMediaIds.get(asset.canonical_version_id);
  }
  const detail = await request(`/api/assets/${asset.id}`).catch(() => null);
  const canonical = (detail?.versions || []).find((version) => version.id === asset.canonical_version_id);
  const mediaId = canonical?.primary_media_asset_id || null;
  state.assetMediaIds.set(asset.canonical_version_id, mediaId);
  return mediaId;
}

/** Thumbnail object URL for a media asset, resolved once per session. */
async function cachedAssetThumbnail(mediaAssetId) {
  if (!mediaAssetId) return null;
  if (state.thumbCache.has(mediaAssetId)) return state.thumbCache.get(mediaAssetId);
  const media = await resolveAssetThumbnail(mediaAssetId).catch(() => null);
  const url = media?.url || null;
  state.thumbCache.set(mediaAssetId, url);
  return url;
}

function hydrateAssetRailThumbs() {
  state.logicalAssets.forEach(async (asset) => {
    const mediaId = await assetCanonicalMediaId(asset).catch(() => null);
    if (!mediaId) return; // graceful: the chip keeps its placeholder glyph
    const url = await cachedAssetThumbnail(mediaId);
    const cell = document.querySelector(`[data-rail-thumb="${CSS.escape(asset.id)}"]`);
    if (url && cell) cell.innerHTML = `<img src="${escapeHTML(url)}" alt="" loading="lazy" />`;
  });
}

/** The asset taxonomy palette. Tokens only, never literals: an inline style
 *  is unreachable by any scope, so a hex here would keep its dark-theme value
 *  on the light rail — and STYLE spent brand amber on a taxonomy label, which
 *  the amber law reserves for CTA, selection, generating and credits. */
function assetKindColor(type) {
  return ({
    CHARACTER: "var(--kind-character)", SCENE: "var(--kind-scene)",
    PRODUCT: "var(--kind-product)", STYLE: "var(--kind-style)",
    WARDROBE: "var(--kind-wardrobe)", PROP: "var(--kind-prop)",
  })[type] || "var(--fg-meta)";
}

function renderProjectStyleLock() {
  const selected = state.logicalAssets.find((asset) => asset.id === $("manualExistingAsset").value);
  const lockable = selected?.asset_type === "STYLE" && selected.canonical_version_id;
  $("lockProjectStyleBtn").disabled = Boolean(state.styleLock?.locked) || !lockable;
  $("lockProjectStyleBtn").textContent = state.styleLock?.locked ? "Project style is locked" : "Lock as the project's style";
  $("projectStyleLockStatus").textContent = state.styleLock?.locked
    ? "Locked. Later shots inherit this look and are checked for drift."
    : (lockable
      ? "Locking is permanent. The look is captured once and every later generation is checked against it."
      : "Make a style version the main reference first, then a project member can lock it explicitly.");
}

async function openAssetDetails(assetId) {
  if (assetId) $("manualExistingAsset").value = assetId;
  await syncManualAssetSelection();
  if (!$("assetDetailsDialog").open) $("assetDetailsDialog").showModal();
}

async function syncManualAssetSelection() {
  const selected = state.logicalAssets.find((asset) => asset.id === $("manualExistingAsset").value);
  renderProjectStyleLock();
  if (!selected) {
    $("assetCurrentName").textContent = "No asset selected";
    $("assetCurrentMeta").textContent = "—";
    $("assetCurrentMedia").innerHTML = '<span class="empty-icon" aria-hidden="true">▣</span>';
    $("assetVersionList").className = "version-list empty-state";
    $("assetVersionList").innerHTML = `
      <div class="empty-block is-compact">
        <p>Pick an asset on the left to see its version history.</p>
      </div>`;
    return;
  }
  $("manualAssetType").value = selected.asset_type;
  $("manualAssetName").value = selected.name;
  $("assetCurrentName").textContent = selected.name;
  $("assetCurrentMeta").textContent = `${simpleLabel(selected.asset_type).toUpperCase()} · ${selected.canonical_version_id ? "main reference set" : "no main reference yet"}`;

  const detail = await request(`/api/assets/${selected.id}`).catch(() => null);
  const versions = detail?.versions || [];

  // Current reference preview: the canonical version's primary media, when it
  // has one; otherwise the placeholder glyph stays.
  const mediaBox = $("assetCurrentMedia");
  mediaBox.innerHTML = '<span class="empty-icon" aria-hidden="true">▣</span>';
  const canonicalVersion = versions.find((version) => version.id === selected.canonical_version_id);
  if (canonicalVersion?.primary_media_asset_id) {
    state.assetMediaIds.set(selected.canonical_version_id, canonicalVersion.primary_media_asset_id);
    cachedAssetThumbnail(canonicalVersion.primary_media_asset_id).then((url) => {
      if (url && $("manualExistingAsset").value === selected.id) {
        mediaBox.innerHTML = `<img src="${escapeHTML(url)}" alt="" />`;
      }
    }).catch(() => null);
  }

  const list = $("assetVersionList");
  if (!versions.length) {
    list.className = "version-list empty-state";
    list.innerHTML = `
      <div class="empty-block is-compact">
        <strong>No versions saved yet</strong>
        <p>Upload a file or promote a generation to create v1.</p>
        <div class="btn-row btn-row-center">
          <button class="btn btn-tertiary" type="button">Upload a version</button>
        </div>
      </div>`;
    // The dialog's own Save button validates the file field first, so with no
    // file chosen the honest next move is the picker, not its error message.
    list.querySelector("button")?.addEventListener("click", () => {
      if ($("manualAssetFile")?.files[0]) $("manualAssetUploadBtn").click();
      else $("manualAssetFile")?.click();
    });
    return;
  }
  list.className = "version-list";
  list.innerHTML = versions.slice().reverse().map((version) => {
    const canonical = version.id === selected.canonical_version_id;
    return `<div class="version-row">
      <span class="version-no mono">v${escapeHTML(String(version.version))}</span>
      <span class="version-label">${escapeHTML(version.label || simpleLabel(version.source))}</span>
      ${canonical
        ? '<span class="status-chip is-ok">Main reference</span>'
        : `<button class="btn btn-tertiary" type="button" data-promote-version="${escapeHTML(version.id)}">Set as main reference</button>`}
    </div>`;
  }).join("");
  list.querySelectorAll("[data-promote-version]").forEach((button) => {
    button.addEventListener("click", guard(() => promoteAssetVersion(selected.id, button.dataset.promoteVersion)));
  });
}

async function promoteAssetVersion(assetId, versionId) {
  await request(`/api/assets/${assetId}/versions/${versionId}/promote`, {
    method: "POST",
    body: JSON.stringify({ reason: "Explicitly set as the canonical reference from asset details" }),
  });
  await loadLogicalAssets();
  $("manualExistingAsset").value = assetId;
  await syncManualAssetSelection();
  toast("Main reference updated. Earlier versions are kept.");
}

async function lockSelectedProjectStyle() {
  if (!state.project) return toast("Create a project first");
  const selected = state.logicalAssets.find((asset) => asset.id === $("manualExistingAsset").value);
  if (selected?.asset_type !== "STYLE" || !selected.canonical_version_id) {
    return toast("Pick a style asset that already has a main reference");
  }
  if (!window.confirm("Locking the project style is permanent and becomes a gate on every later generation. Continue?")) return;
  state.styleLock = await request(`/api/projects/${state.project.id}/style-lock`, {
    method: "POST",
    body: JSON.stringify({
      style_version_id: selected.canonical_version_id,
      reason: "Explicitly confirmed by a project member from asset details",
      explicit_confirmation: true,
    }),
  });
  state.project.canonical_style_version_id = state.styleLock.style_version_id;
  renderProjectStyleLock();
  toast("Style locked. Every later shot inherits it and is checked for drift.");
}

async function uploadManualAssetVersion() {
  if (!state.project) return toast("Create a project first");
  const file = $("manualAssetFile").files[0];
  if (!file) return toast("Choose the replacement image");
  const existing = state.logicalAssets.find((asset) => asset.id === $("manualExistingAsset").value);
  const assetType = existing?.asset_type || $("manualAssetType").value;
  const assetName = (existing?.name || $("manualAssetName").value).trim();
  if (!assetName) return toast("Give the asset a name");
  const button = $("manualAssetUploadBtn");
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const form = new FormData();
    form.append("project_id", state.project.id);
    form.append("asset_type", userUploadMediaType(assetType));
    form.append("file", file);
    const mediaResponse = await fetch(`${API}/v1/assets`, {
      method: "POST", body: form, credentials: "include", headers: csrfHeaders("POST"),
    });
    if (!mediaResponse.ok) {
      const detail = await mediaResponse.json().catch(() => ({}));
      throw new Error(detail.detail || "Image upload failed");
    }
    const media = await mediaResponse.json();
    const logical = existing || await request("/api/assets", {
      method: "POST",
      body: JSON.stringify({ project_id: state.project.id, asset_type: assetType, name: assetName }),
    });
    const version = await request(`/api/assets/${logical.id}/versions`, {
      method: "POST",
      body: JSON.stringify({
        primary_media_asset_id: media.id,
        parent_version_id: existing?.canonical_version_id || null,
        label: `User upload · ${new Date().toLocaleString()}`,
        source: "USER_UPLOAD",
        status: "READY",
      }),
    });
    let promoted = false;
    if ($("manualPromoteCanonical").checked) {
      await request(`/api/assets/${logical.id}/versions/${version.id}/promote`, {
        method: "POST",
        body: JSON.stringify({ reason: "Explicitly set as the canonical reference on upload" }),
      });
      promoted = true;
    }
    await loadLogicalAssets();
    $("manualExistingAsset").value = logical.id;
    await syncManualAssetSelection();
    $("manualAssetFile").value = "";
    $("manualAssetStatus").textContent = `Saved ${simpleLabel(assetType)} "${assetName}" as v${version.version}${promoted ? " and set it as the main reference." : ". The main reference did not change."}`;
    toast("New version saved. Earlier versions stay traceable.");
  } catch (error) {
    // The dialog is modal; leaving its own status line on the default helper
    // text after a failure reads as "nothing happened".
    $("manualAssetStatus").className = "output-box is-error";
    $("manualAssetStatus").textContent = error.message;
    throw error;
  } finally {
    button.disabled = false;
    button.textContent = "Save as new version";
  }
}

async function selectProject(id) {
  if (!id) return;
  if (state.project && state.project.id !== id) {
    state.passengerReferenceUpload = null;
    state.passengerJobs = { image: null, video: null };
    state.confirmedAssets.clear();
    stopPassengerPolling();
    $("passengerReference").value = "";
    $("referenceFileName").hidden = true;
    renderPassengerJob(null);
  }
  const projectChanged = state.project?.id !== id;
  state.project = await request(`/v1/projects/${id}`);
  $("projectSelect").value = id;
  if (projectChanged) {
    state.creative.session = null;
    renderCreative();
    // The catalogues' plan locks belong to this project's workspace.
    Promise.all([loadPassengerModels(), loadImageTiers()]).catch(() => null);
  }
  await loadLogicalAssets();
  await loadCharacters();
  await loadEpisodeStrip();
  const keepEpisode = !projectChanged && state.episode
    && state.project.episodes.some((episode) => episode.id === state.episode.id);
  if (keepEpisode) await loadEpisode(state.episode.id);
  else if (state.project.episodes.length) await loadEpisode(state.project.episodes[0].id);
  else resetProductionView();
  if (!state.passengerJobs[state.passengerMedia]) await renderPassengerJob(null);
  if (state.page === "ai-director") await loadCreativeSessions();
  if (state.page === "productions") refreshProductions().catch(() => null);
  syncOperationsContext();
}

/* ============================================================
   Director page
   ============================================================ */
let shotStageObjectUrl = null;

/** Resolve a media asset to something an <img>/<video> can show. */
async function resolveAssetMedia(assetId) {
  if (!assetId) return null;
  const asset = await request(`/v1/assets/${assetId}`).catch(() => null);
  if (!asset) return null;
  let url = asset.public_url || "";
  const isProtectedLocalMedia = asset.storage_key && (() => {
    try { return new URL(asset.public_url, location.href).pathname.includes("/v1/storage/"); }
    catch (_error) { return false; }
  })();
  if (isProtectedLocalMedia) {
    const storagePath = asset.storage_key.split("/").map(encodeURIComponent).join("/");
    const response = await fetch(`${API}/v1/storage/${storagePath}`, { credentials: "include" });
    if (!response.ok) return null;
    url = URL.createObjectURL(await response.blob());
    return { url, mime: asset.mime_type || "", revocable: true };
  }
  return url ? { url, mime: asset.mime_type || "", revocable: false } : null;
}

async function resolveAssetThumbnail(assetId) {
  // Galleries read the derived thumbnail, not the original: a grid of 4K
  // plates must not download 4K plates. Falls back to the full asset only
  // when no thumbnail can be derived (odd media types).
  if (!assetId) return null;
  try {
    const response = await fetch(`${API}/v1/assets/${assetId}/thumbnail`, { credentials: "include" });
    if (response.ok) {
      return { url: URL.createObjectURL(await response.blob()), mime: "image/jpeg", revocable: true };
    }
  } catch (_error) { /* fall through to the original */ }
  return resolveAssetMedia(assetId);
}

function resetProductionView() {
  state.episode = null; state.shot = null; state.candidates = [];
  $("scriptPanel").hidden = false;
  $("shotTreePanel").hidden = true;
  $("viewScriptBtn").hidden = true;
  $("compileBtn").disabled = !state.project;
  $("scriptInput").disabled = !state.project;
  $("sceneList").className = "shot-tree empty-state";
  $("sceneList").textContent = "Break a script into shots to see them here.";
  $("shotTimeline").className = "filmstrip empty-state";
  $("shotTimeline").textContent = "No shots yet";
  renderNoShotSelected();
  renderCandidates([]);
  syncOperationsContext();
}

async function loadEpisode(id) {
  state.episode = await request(`/v1/episodes/${id}`);
  $("scriptInput").value = state.episode.script_source || "";
  $("scriptOriginalView").textContent = state.episode.script_source || "No script recorded for this episode.";
  $("episodeStatus").textContent = simpleLabel(state.episode.status);
  renderEpisodeStrip();
  renderScenes(); renderShots();
  const firstShot = state.episode.scenes.flatMap((scene) => scene.shots)[0];
  if (firstShot) await selectShot(firstShot.id);
  else renderNoShotSelected();
}

function shotTone(shot) {
  if (shot.status === "COMMITTED") return "is-ok";
  if (["GENERATING", "RUNNING"].includes(shot.status)) return "is-running";
  return "";
}

/** Once shots exist the sidebar becomes a scene/shot tree; the script moves to a drawer. */
function renderScenes() {
  const scenes = state.episode?.scenes || [];
  const shots = scenes.flatMap((scene) => scene.shots);
  $("sceneCount").textContent = scenes.length;
  $("scriptPanel").hidden = shots.length > 0;
  $("shotTreePanel").hidden = shots.length === 0;
  $("viewScriptBtn").hidden = shots.length === 0;

  const tree = $("sceneList");
  if (!scenes.length) {
    tree.className = "shot-tree empty-state";
    tree.textContent = "Break a script into shots to see them here.";
    return;
  }
  let counter = 0;
  tree.className = "shot-tree";
  tree.innerHTML = scenes.map((scene) => `
    <div class="tree-scene">
      <div class="tree-scene-head">
        <b>SC ${String(scene.sequence).padStart(2, "0")}</b>
        <span>${escapeHTML(scene.description || "Untitled scene")}</span>
      </div>
      ${scene.shots.map((shot) => {
        counter += 1;
        const no = String(counter).padStart(2, "0");
        return `<button class="tree-shot ${state.shot?.id === shot.id ? "active" : ""}" type="button" data-shot="${escapeHTML(shot.id)}">
          <span class="tree-no">SH ${no}</span>
          <span class="tree-name">${escapeHTML(shot.prompt || shot.user_prompt || "Untitled shot")}</span>
          <span class="tree-dot ${shotTone(shot)}"></span>
        </button>`;
      }).join("")}
    </div>`).join("");
  tree.querySelectorAll("[data-shot]").forEach((button) => {
    button.addEventListener("click", guard(() => selectShot(button.dataset.shot)));
  });
}

function renderShots() {
  const shots = state.episode?.scenes.flatMap((scene) => scene.shots) || [];
  const strip = $("shotTimeline");
  if (!shots.length) {
    strip.className = "filmstrip empty-state";
    strip.textContent = "No shots yet";
    return;
  }
  strip.className = "filmstrip";
  strip.innerHTML = shots.map((shot, index) => `
    <button class="strip-shot ${state.shot?.id === shot.id ? "active" : ""}" type="button" data-strip-shot="${escapeHTML(shot.id)}">
      <span>SH ${String(index + 1).padStart(2, "0")}</span>
      <strong>${escapeHTML(shot.prompt || shot.user_prompt || "Untitled")}</strong>
      <small>${shot.duration}s · ${simpleLabel(shot.status)}</small>
    </button>`).join("");
  strip.querySelectorAll("[data-strip-shot]").forEach((button) => {
    button.addEventListener("click", guard(() => selectShot(button.dataset.stripShot)));
  });
}

function renderNoShotSelected() {
  const hasShots = (state.episode?.scenes || []).some((scene) => scene.shots.length);
  $("directorBreadcrumb").textContent = "No shot selected";
  $("directorTimeContext").textContent = "";
  $("directorShotStatus").className = "status-chip is-neutral";
  $("directorShotStatus").textContent = "Draft";
  // Exactly one primary; three different situations, three different next moves.
  const empty = !state.project
    ? {
      icon: ICON_PROJECT,
      title: "Scenes belong to a project",
      body: "Create one and the script you paste becomes an ordered, producible shot list.",
      cta: '<button class="btn btn-primary" type="button" data-empty-action="new-project">Create a project</button>',
    }
    : hasShots
      ? {
        icon: ICON_FRAME,
        title: "Pick a shot to direct it",
        body: "The approved take for the selected shot shows here; variants for comparison sit below.",
        cta: '<button class="btn btn-primary" type="button" data-empty-action="first-shot">Open the first shot</button>',
      }
      : {
        icon: ICON_FRAME,
        title: "Break a script into shots",
        body: "Paste a script on the left. BestShiny splits it into scenes and shots and remembers the state each shot starts and ends in.",
        cta: '<button class="btn btn-primary" type="button" data-empty-action="compile">Break into shots</button>',
      };
  // The scope attribute on #shotPreviewStage survives this className rewrite;
  // the state class is what the redesign's canvas rules key off.
  $("shotPreviewStage").className = "shot-stage is-empty";
  $("shotStageMedia").innerHTML = `
    <div class="empty-block">
      <span class="empty-icon" aria-hidden="true">${empty.icon}</span>
      <strong>${empty.title}</strong>
      <p>${empty.body}</p>
      <div class="btn-row btn-row-center">${empty.cta}</div>
    </div>`;
  $("shotNumber").textContent = "SHOT —";
  $("shotAction").textContent = "Select a shot";
  $("shotState").textContent = "Opening state → one action → closing state";
  $("shotTitle").textContent = "No shot selected";
  $("shotPrompt").textContent = "Break a script into shots on the left. BestShiny builds an ordered shot list and remembers the state each shot starts and ends in.";
  ["shotDuration", "shotContinuity", "shotPolicy", "shotProvider"].forEach((id) => { $(id).textContent = "—"; });
  ["compShotType", "compInputState", "compOutputState"].forEach((id) => { $(id).textContent = "—"; });
  ["shotModelProvider", "shotModelPolicy", "shotModelContinuity"].forEach((id) => { $(id).textContent = "—"; });
  $("barShotDuration").textContent = "—";
  $("barShotModel").textContent = "—";
  $("generateBtn").disabled = true;
  $("generateBtn").textContent = "Generate shot";
  $("rawPrompt").value = "";
  $("compiledPrompt").value = "";
}

async function selectShot(id) {
  state.shot = await request(`/v1/shots/${id}`);
  renderScenes();
  renderShots();
  const scenes = state.episode?.scenes || [];
  const allShots = scenes.flatMap((scene) => scene.shots);
  const index = allShots.findIndex((shot) => shot.id === id) + 1;
  const scene = scenes.find((item) => item.shots.some((shot) => shot.id === id));
  const shot = state.shot;

  $("directorBreadcrumb").textContent = `Scene ${String(scene?.sequence ?? 1).padStart(2, "0")} · Shot ${String(index).padStart(2, "0")}`;
  $("directorTimeContext").textContent = scene?.time_context || "";
  const tone = statusTone(shot.status);
  $("directorShotStatus").className = `status-chip ${tone}`;
  $("directorShotStatus").textContent = simpleLabel(shot.status);

  $("shotNumber").textContent = `SHOT ${String(index).padStart(2, "0")}`;
  $("shotAction").textContent = shot.user_prompt;
  $("shotState").textContent = `${shot.input_state ? "Opening state set" : "No opening state"} → one action → ${shot.output_state ? "closing state planned" : "no closing state"}`;
  $("shotTitle").textContent = `${simpleLabel(shot.shot_type)} · Shot ${index}`;
  $("shotPrompt").textContent = shot.user_prompt;
  $("shotDuration").textContent = `${shot.duration}s`;
  $("shotContinuity").textContent = simpleLabel(shot.continuity_policy);
  $("shotPolicy").textContent = simpleLabel(shot.generation_policy);
  // A provider string is not a model id, so the Director can only name the
  // route tier. Naming a vendor here would promise a model nobody resolved.
  $("shotProvider").textContent = routeName(shot.provider);
  $("compShotType").textContent = simpleLabel(shot.shot_type);
  $("compInputState").textContent = shot.input_state ? "Set" : "Not set";
  $("compOutputState").textContent = shot.output_state ? "Planned" : "Not set";
  $("shotModelProvider").textContent = routeName(shot.provider);
  $("shotModelPolicy").textContent = simpleLabel(shot.generation_policy);
  $("shotModelContinuity").textContent = simpleLabel(shot.continuity_policy);
  $("rawPrompt").value = shot.user_prompt;
  $("compiledPrompt").value = shot.compiled_prompt || "";
  $("barShotDuration").textContent = `${shot.duration}s`;
  $("barShotModel").textContent = routeName(shot.provider);
  $("generateBtn").disabled = false;
  $("generateBtn").textContent = shot.status === "COMMITTED" ? "Regenerate shot" : "Generate shot";
  // Only a shot with no paid history can be deleted; the server refuses the rest.
  $("shotDeleteBtn").hidden = Boolean(shot.committed_candidate_id) || ["QUEUED", "GENERATING", "VALIDATING", "COMMITTED"].includes(shot.status);

  await loadCandidates();
  await renderShotStage();
  syncOperationsContext();
}

async function deleteSelectedShot() {
  const shot = state.shot;
  if (!shot) return;
  if (!window.confirm("Delete this shot? Only a shot that has never been generated can be deleted.")) return;
  await request(`/v1/shots/${shot.id}`, { method: "DELETE" });
  state.shot = null;
  $("shotDeleteBtn").hidden = true;
  if (state.episode?.id) await loadEpisode(state.episode.id);
  toast("Shot deleted");
}

/** The current shot carries the page: show the approved take when there is one. */
async function renderShotStage() {
  const stage = $("shotStageMedia");
  if (shotStageObjectUrl) { URL.revokeObjectURL(shotStageObjectUrl); shotStageObjectUrl = null; }
  const committed = state.candidates.find((candidate) => candidate.status === "COMMITTED" && candidate.output_asset_id)
    || state.candidates.find((candidate) => candidate.output_asset_id);
  const shotFrame = $("shotPreviewStage");
  if (!committed) {
    const generating = state.candidates.some((candidate) => ["QUEUED", "RUNNING", "GENERATING", "VALIDATING"].includes(candidate.status));
    shotFrame.className = `shot-stage ${generating ? "is-generating" : "is-empty"}`;
    // Shot candidates arrive as a SET with no per-candidate signal, so the
    // bar here is always indeterminate: no number, no aria-valuenow, no
    // claim about how far along it is. The count of active variants is real
    // signal though, so it still gets a line.
    const active = state.candidates.filter((candidate) => ["QUEUED", "RUNNING", "GENERATING", "VALIDATING"].includes(candidate.status)).length;
    stage.innerHTML = generating
      ? `
      <div class="canvas-generating">
        <div class="gen-bloom" aria-hidden="true"></div>
        <div class="gen-gate" aria-hidden="true"><i></i><i></i><i></i></div>
        <p class="gen-title" role="status">Shooting this shot</p>
        <div class="gen-track is-indeterminate" role="progressbar" aria-label="Generation progress"
             aria-valuemin="0" aria-valuemax="100"><i class="gen-fill"></i></div>
        <p class="gen-note">${active} variant${active === 1 ? "" : "s"} rendering. Nothing is approved until you approve it.</p>
      </div>`
      : `
      <div class="empty-block">
        <span class="empty-icon" aria-hidden="true">${ICON_FRAME}</span>
        <strong>This shot hasn't been shot yet</strong>
        <p>Generate it to see the take here, then approve one variant into the timeline.</p>
        <div class="btn-row btn-row-center">
          <button class="btn btn-primary" type="button" data-empty-action="generate-shot">Generate this shot</button>
        </div>
      </div>`;
    return;
  }
  const media = await resolveAssetMedia(committed.output_asset_id);
  if (!media) {
    shotFrame.className = "shot-stage is-empty";
    stage.innerHTML = `
      <div class="empty-block">
        <span class="empty-icon" aria-hidden="true">${ICON_ALERT}</span>
        <strong>This take will not display</strong>
        <p>The file exists but nothing could be loaded from it. Refreshing the variants usually recovers it.</p>
      </div>`;
    return;
  }
  if (media.revocable) shotStageObjectUrl = media.url;
  shotFrame.className = "shot-stage has-result";
  stage.innerHTML = media.mime.startsWith("video/")
    ? `<video src="${escapeHTML(media.url)}" controls playsinline></video>`
    : `<img src="${escapeHTML(media.url)}" alt="Approved take for this shot" />`;
}

async function loadCandidates() {
  if (!state.shot) return;
  state.candidates = await request(`/v1/shots/${state.shot.id}/candidates`);
  const candidateJobId = state.candidates.find((candidate) => candidate.generation_job_id)?.generation_job_id;
  if (candidateJobId && !$("operationsJobId").value.trim()) $("operationsJobId").value = candidateJobId;
  state.candidates.forEach((candidate) => {
    if (candidate.generation_job_id) {
      rememberJob({ id: candidate.generation_job_id, status: candidate.status, shotLabel: state.shot?.user_prompt, cost: candidate.cost });
    }
  });
  renderCandidates(state.candidates);
  syncOperationsContext();
}

/** What the automated checks actually found, in the words a director would
 *  use. The raw enum is the backend's vocabulary, not the creator's. */
const QA_SUMMARY = {
  IDENTITY_DRIFT: "The character does not look like their reference.",
  ACTION_MISMATCH: "The action does not match what the shot asked for.",
  CAMERA_MISMATCH: "The framing or move is not what was planned.",
  LOW_CONFIDENCE: "The checks could not decide on their own.",
};

function renderCandidates(candidates) {
  const grid = $("candidateGrid");
  if (!candidates.length) {
    grid.className = "variant-grid empty-state";
    grid.innerHTML = `
      <div class="empty-block">
        <span class="empty-icon" aria-hidden="true">${ICON_FRAME}</span>
        <strong>No takes to compare yet</strong>
        <p>Each generation returns A / B / C with identity, camera and action checks, so you can pick the one that holds.</p>
        <div class="btn-row btn-row-center">
          <button class="btn btn-primary" type="button" data-empty-action="generate-shot">Generate this shot</button>
        </div>
      </div>`;
    return;
  }
  grid.className = "variant-grid";
  grid.innerHTML = candidates.map((candidate, index) => {
    const qa = candidate.qa || {};
    const needsHumanReview = candidate.status === "USER_REVIEW_REQUIRED";
    const canCommit = candidate.status === "PASSED";
    const reviewBlocked = ["HARD_FAILED", "REJECTED", "COMMITTED"].includes(candidate.status);
    const scores = [
      ["Overall", Math.round((qa.overall_score || 0) * 100)],
      ["Character", Math.round((qa.character_score || 0) * 100)],
      ["Camera", Math.round((qa.camera_score || 0) * 100)],
      ["Action", Math.round((qa.action_score || 0) * 100)],
    ];
    const humanReview = needsHumanReview && !reviewBlocked ? `
      <section class="review-box" aria-label="Human review">
        <strong>This one needs your eyes</strong>
        <p>Automated checks do not have enough to decide. Check identity, action and the join to the previous shot, then say why it passes.</p>
        <label class="field"><textarea data-review-reason="${escapeHTML(candidate.id)}" rows="3" placeholder="e.g. Verified identity, eyeline and the join to shot 03. Usable."></textarea></label>
        <label class="check-row"><input type="checkbox" data-review-confirm="${escapeHTML(candidate.id)}" /> I reviewed this result myself and confirm it can proceed</label>
        <button class="btn btn-secondary btn-full" data-human-review="${escapeHTML(candidate.id)}" disabled>Confirm review</button>
      </section>` : "";
    const validateAction = needsHumanReview || reviewBlocked ? "" : `<button class="btn btn-secondary btn-grow" data-validate="${escapeHTML(candidate.id)}">Run checks</button>`;
    const commitAction = canCommit ? `<button class="btn btn-secondary btn-grow" data-commit="${escapeHTML(candidate.id)}">Approve</button>` : "";
    return `<article class="variant ${candidate.status === "COMMITTED" ? "is-committed" : ""}">
      <div class="variant-head">
        <span>Variant ${String.fromCharCode(65 + index)}</span>
        <span class="status-chip ${statusTone(candidate.status)}">${simpleLabel(candidate.status)}</span>
      </div>
      <div class="score-bars">${scores.map(([name, value]) => `
        <div class="score-row ${value >= 75 ? "is-strong" : ""}"><span>${name}</span><div class="bar"><i style="width:${value}%"></i></div><b>${value}</b></div>`).join("")}</div>
      <div class="output-box">${escapeHTML(qa.summary
        ? (QA_SUMMARY[qa.summary] || sentenceCase(humanizeCode(qa.summary)))
        : "Waiting for generation or checks")}<br>${Math.max(1, Math.ceil(candidate.cost / .01))} credits</div>
      ${humanReview}
      <div class="variant-actions">${validateAction}${commitAction}</div>
    </article>`;
  }).join("");

  grid.querySelectorAll("[data-validate]").forEach((button) => button.addEventListener("click", guard(() => validateCandidate(button.dataset.validate))));
  grid.querySelectorAll("[data-commit]").forEach((button) => button.addEventListener("click", guard(() => commitCandidate(button.dataset.commit))));
  grid.querySelectorAll("[data-human-review]").forEach((button) => button.addEventListener("click", guard(() => humanReviewCandidate(button.dataset.humanReview))));
  grid.querySelectorAll("[data-review-reason]").forEach((input) => input.addEventListener("input", () => updateHumanReviewControl(input.dataset.reviewReason)));
  grid.querySelectorAll("[data-review-confirm]").forEach((input) => input.addEventListener("change", () => updateHumanReviewControl(input.dataset.reviewConfirm)));
}

function updateHumanReviewControl(candidateId) {
  const reason = document.querySelector(`[data-review-reason="${candidateId}"]`);
  const confirmation = document.querySelector(`[data-review-confirm="${candidateId}"]`);
  const button = document.querySelector(`[data-human-review="${candidateId}"]`);
  if (button) button.disabled = !reason?.value.trim() || !confirmation?.checked;
}

async function compileScript() {
  if (!state.project) { toast("Create a project first"); return; }
  const script = $("scriptInput").value.trim();
  if (!script) { toast("Paste a script first"); return; }
  let episodeId = state.episode?.id || state.project.episodes[0]?.id;
  if (!episodeId) {
    const episode = await request(`/v1/projects/${state.project.id}/episodes`, {
      method: "POST",
      body: JSON.stringify({ project_id: state.project.id, title: "Episode 1", episode_number: 1, script_source: script }),
    });
    episodeId = episode.id;
  } else if (state.episode?.script_source !== script) {
    toast("Your existing shots are protected, so the script is not overwritten. Start a new project to break down a different script.");
    return;
  }
  await request(`/v1/episodes/${episodeId}/compile`, { method: "POST", body: "{}" });
  await selectProject(state.project.id);
  toast("Script broken into scenes and shots. The join between each shot is recorded.");
}

async function generateShot() {
  if (!state.shot) { toast("Select a shot first"); return; }
  const projectId = state.project.id;
  const shotId = state.shot.id;
  const estimatedCost = Number($("estimatedCost").value || 0);
  const fingerprint = JSON.stringify({ projectId, shotId, estimatedCost });
  const idempotencyKey = beginSubmission("shot", fingerprint);
  if (!idempotencyKey) return;
  const button = $("generateBtn");
  button.disabled = true;
  button.textContent = "Submitting…";
  let succeeded = false;
  try {
    await request(`/v1/shots/${shotId}/generate`, {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idempotencyKey, estimated_cost: estimatedCost }),
    });
    await loadCandidates();
    await renderShotStage();
    await loadCredits();
    succeeded = true;
    toast("Variants queued. A network retry reuses the same submission instead of charging twice.");
  } finally {
    finishSubmission("shot", idempotencyKey, succeeded);
    button.disabled = false;
    button.textContent = state.shot?.status === "COMMITTED" ? "Regenerate shot" : "Generate shot";
  }
}

async function validateCandidate(id) {
  await request(`/v1/shots/${state.shot.id}/candidates/${id}/validate`, { method: "POST", body: JSON.stringify({ evidence: {} }) });
  await loadCandidates();
  toast("Checks run. Where visual evidence is thin, the result is sent to you instead of auto-passing.");
}

async function humanReviewCandidate(id) {
  const candidate = state.candidates.find((item) => item.id === id);
  if (!candidate || candidate.status !== "USER_REVIEW_REQUIRED") {
    await loadCandidates();
    return toast("That variant has moved on. Continue from its current state.");
  }
  const reason = document.querySelector(`[data-review-reason="${id}"]`)?.value.trim() || "";
  const explicitConfirmation = document.querySelector(`[data-review-confirm="${id}"]`)?.checked === true;
  if (!reason) return toast("Say why it passes");
  if (!explicitConfirmation) return toast("Tick the explicit confirmation");
  const button = document.querySelector(`[data-human-review="${id}"]`);
  if (button) { button.disabled = true; button.textContent = "Submitting…"; }
  try {
    await request(`/v1/shots/${state.shot.id}/candidates/${id}/human-review`, {
      method: "POST",
      body: JSON.stringify({ reason, explicit_confirmation: true }),
    });
    await loadCandidates();
    toast("Review recorded. The variant can now be approved.");
  } catch (error) {
    if (button) { button.textContent = "Confirm review"; updateHumanReviewControl(id); }
    throw error;
  }
}

async function commitCandidate(id) {
  await request(`/v1/shots/${state.shot.id}/candidates/${id}/commit`, { method: "POST", body: "{}" });
  await selectShot(state.shot.id);
  toast("Variant approved and written into the timeline.");
}

async function createCharacter() {
  if (!state.project) return;
  const name = $("characterName").value.trim();
  if (!name) return toast("Give the character a name");
  const character = await request("/v1/characters", {
    method: "POST",
    body: JSON.stringify({ project_id: state.project.id, name, description: $("characterDescription").value }),
  });
  state.selectedCharacterId = character.id;
  await loadCharacters();
  toast("Character created. Upload a master reference to lock its identity.");
}

async function loadCharacters() {
  if (!state.project) return;
  state.characters = await request(`/v1/projects/${state.project.id}/characters`);
  if (!state.characters.some((character) => character.id === state.selectedCharacterId)) {
    state.selectedCharacterId = state.characters[0]?.id || null;
  }
  renderCharacters();
}

function renderCharacters() {
  const list = $("characterList");
  list.innerHTML = state.characters.length ? state.characters.map((character) => {
    const latest = character.identity_versions.at(-1);
    const selected = character.id === state.selectedCharacterId ? " selected" : "";
    const identity = latest
      ? `Identity v${latest.version} locked · upload to create v${latest.version + 1}`
      : "No master reference locked yet";
    return `<button class="binding${selected}" type="button" data-character="${character.id}"><strong>${escapeHTML(character.name)}</strong><span>${identity}</span></button>`;
  }).join("") : CHARACTERS_EMPTY;
  bindCharactersEmptyCta(list);
  list.querySelectorAll("[data-character]").forEach((button) => button.addEventListener("click", () => {
    state.selectedCharacterId = button.dataset.character;
    renderCharacters();
    syncOperationsContext();
  }));
}

async function confirmCharacterIdentity() {
  if (!state.project || !state.selectedCharacterId) return toast("Create and select a character first");
  const file = $("characterAsset").files[0];
  if (!file) return toast("Choose a reference image");
  const form = new FormData();
  form.append("project_id", state.project.id);
  form.append("asset_type", "CHARACTER_MASTER");
  form.append("character_id", state.selectedCharacterId);
  form.append("file", file);
  const upload = await fetch(`${API}/v1/assets`, {
    method: "POST", body: form, credentials: "include", headers: csrfHeaders("POST"),
  });
  if (!upload.ok) {
    const detail = await upload.json().catch(() => ({ detail: upload.statusText }));
    throw new Error(detail.detail || "Character image upload failed");
  }
  const asset = await upload.json();
  const identity = await request(`/v1/characters/${state.selectedCharacterId}/confirm-identity`, {
    method: "POST", body: JSON.stringify({ master_asset_id: asset.id }),
  });
  $("characterAsset").value = "";
  await loadCharacters();
  toast(`Identity v${identity.version} locked. Earlier versions are kept.`);
}

async function continuity() {
  if (!state.shot) return toast("Select a shot first");
  const isReverse = $("cameraAngle").value === "Profile" && $("cameraMove").value === "Orbit";
  const result = await request(`/v1/shots/${state.shot.id}/continuity`, {
    method: "POST",
    body: JSON.stringify({
      project_id: state.project.id,
      risk: {
        camera_axis_delta: isReverse ? .8 : .12,
        camera_angle_delta: isReverse ? .7 : .15,
        action_continuity: .9,
        previous_frame_quality: .85,
      },
    }),
  });
  const reasons = result.reasons.map((reason) => simpleLabel(reason)).join(" · ");
  const risk = Math.round(result.risk_score * 100);
  $("continuityResult").className = "output-box";
  $("continuityResult").innerHTML = `<strong>${escapeHTML(simpleLabel(result.mode))}</strong><br>Risk ${risk} / 100 · ${escapeHTML(reasons)}`;
}

/* ============================================================
   Productions — user-facing job list and "My creations" gallery.

   The durable source is GET /v1/generations?project_id=…, so the history
   survives a reload; jobs the session touched directly are merged on top.
   ============================================================ */
function rememberJob(job) {
  if (!job?.id) return;
  const previous = state.jobs.get(job.id) || {};
  // A job remembered from a surface that does not carry project_id (shot
  // candidates, the generate response) belongs to the project that surface
  // was showing; stamping it here keeps it out of every other project's list.
  const projectId = job.project_id || previous.project_id || state.project?.id || null;
  state.jobs.set(job.id, { ...previous, ...job, project_id: projectId });
}

const JOB_BUCKET = {
  RUNNING: "running", GENERATING: "running", SUBMITTED: "running", VALIDATING: "running",
  QUEUED: "queued", NEW: "queued", RESERVED: "queued", RETRY_WAIT: "queued",
  COMPLETED: "completed", COMMITTED: "completed", PASSED: "completed",
  FAILED: "failed", HARD_FAILED: "failed", CANCELLED: "failed",
};
const bucketOf = (job) => JOB_BUCKET[job.status] || "queued";

function jobProgress(job) {
  // Real progress when the job carries it (the create-canvas poll writes it);
  // the coarse per-bucket estimate only as a fallback.
  if (typeof job.progress === "number" && Number.isFinite(job.progress)) {
    return Math.max(0, Math.min(100, Math.round(job.progress)));
  }
  const bucket = bucketOf(job);
  // A finished row is 100% by definition. An in-flight row with no reported
  // progress returns null, which the renderer draws as an indeterminate
  // shimmer instead of inventing a percentage.
  if (bucket === "completed" || bucket === "failed") return 100;
  return null;
}

/** Seed and refresh state.jobs from the server's per-project listing, so
 *  productions survive a reload instead of living only in session memory. */
async function loadProjectGenerations() {
  if (!state.project) return new Set();
  const listing = await request(
    `/v1/generations?project_id=${encodeURIComponent(state.project.id)}&limit=100`,
  ).catch(() => null);
  const jobs = listing?.jobs || [];
  jobs.forEach((job) => rememberJob(job));
  return new Set(jobs.map((job) => job.id));
}

async function refreshProductions() {
  // Jobs this session remembered that the capped listing did not return
  // (older shot candidates, or anything past the 100 newest) are refreshed
  // by id so a remembered status never freezes; bounded so a long session
  // cannot fan out.
  const listed = await loadProjectGenerations();
  const stale = projectJobs()
    .filter((job) => !listed.has(job.id) && !TERMINAL_JOB_STATES.has(job.status))
    .slice(0, 20);
  const fresh = await Promise.all(
    stale.map((job) => request(`/v1/generations/${encodeURIComponent(job.id)}`).catch(() => null)),
  );
  fresh.forEach((job) => { if (job) rememberJob(job); });
  renderProductions();
}

/** Jobs that belong on this project's surfaces, newest first. Every
 *  remembered job carries a project_id (stamped on remember), so nothing
 *  from another project can leak in; without a project there is nothing. */
function projectJobs() {
  if (!state.project) return [];
  return [...state.jobs.values()]
    .filter((job) => job.project_id === state.project.id)
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
}

function renderProductions() {
  const list = $("productionsList");
  if (!list) return;
  // The rows are about to be replaced; an open menu anchored to one of them
  // would be orphaned, so it closes with the redraw.
  closeJobMenu();
  const jobs = projectJobs();
  const counts = { running: 0, queued: 0, completed: 0, failed: 0 };
  jobs.forEach((job) => { counts[bucketOf(job)] += 1; });
  $("prodCountRunning").textContent = counts.running;
  $("prodCountQueued").textContent = counts.queued;
  $("prodCountCompleted").textContent = counts.completed;
  $("prodCountFailed").textContent = counts.failed;
  $("barJobCount").textContent = `${jobs.length} creation${jobs.length === 1 ? "" : "s"}`;
  // The quoted credits of this project's jobs that ran or are running. Failed
  // and cancelled jobs are left out: a pre-submission failure is refunded
  // server-side and the job row keeps its quote, so counting it would show
  // credits the workspace got back. This is a quote total, not a ledger.
  const spendCredits = jobs
    .filter((job) => bucketOf(job) !== "failed")
    .reduce((total, job) => total + jobCredits(job), 0);
  $("barJobSpend").textContent = `${spendCredits} credits`;
  renderCreationsGallery(jobs);

  const visible = state.jobFilter === "all" ? jobs : jobs.filter((job) => bucketOf(job) === state.jobFilter);
  if (!visible.length) {
    list.className = "job-list empty-state";
    // Two different emptinesses: nothing made at all, or nothing in the state
    // the filter is asking for. They need different next moves.
    list.innerHTML = jobs.length
      ? `
      <div class="empty-block">
        <span class="empty-icon" aria-hidden="true">${ICON_FRAME}</span>
        <strong>No creations in this state</strong>
        <p>You have ${jobs.length} creation${jobs.length === 1 ? "" : "s"} in other states.</p>
        <div class="btn-row btn-row-center">
          <button class="btn btn-primary" type="button" data-empty-action="show-all-jobs">Show all</button>
        </div>
      </div>`
      : `
      <div class="empty-block">
        <span class="empty-icon" aria-hidden="true">${ICON_FRAME}</span>
        <strong>Nothing has been produced yet</strong>
        <p>Every frame and shot you generate appears here with its progress, its cost and a way to recover it.</p>
        <div class="btn-row btn-row-center">
          <button class="btn btn-primary" type="button" data-empty-action="go-create">Go to Create</button>
          <button class="btn btn-tertiary" type="button" data-empty-action="go-director">Open Director</button>
        </div>
      </div>`;
    return;
  }
  list.className = "job-list";
  list.innerHTML = visible.map((job) => {
    const bucket = bucketOf(job);
    const tone = statusTone(job.status);
    const credits = jobCredits(job);
    return `<div class="job-row">
      <button class="job-card ${state.selectedJobId === job.id ? "active" : ""}" type="button" data-job="${escapeHTML(job.id)}">
      <span class="job-rail ${tone}"></span>
      <span class="job-main">
        <span class="job-title">
          <strong>${escapeHTML(job.shotLabel || friendlyModel(job.model))}</strong>
          <span class="status-chip ${tone}">${simpleLabel(job.status)}</span>
        </span>
        <span class="job-sub mono">${escapeHTML(job.id)}</span>
      </span>
      <span class="job-side">
        ${bucket === "running" || bucket === "queued"
          ? (jobProgress(job) === null
            // No signal from this route yet — the row shimmers rather than showing a
            // number the provider never gave us. Same honesty rule as the canvas.
            ? '<span class="job-progress indeterminate"><i></i></span>'
            : `<span class="job-progress"><i style="width:${jobProgress(job)}%"></i></span>`)
          : ""}
        <span class="job-cost mono">${credits ? `${credits} credits` : "—"}</span>
      </span>
      </button>
      <button class="job-menu" type="button" aria-haspopup="menu" aria-expanded="false"
              data-job-menu="${escapeHTML(job.id)}" title="More actions"
              aria-label="More actions for this creation">&#8943;</button>
    </div>`;
  }).join("");
  list.querySelectorAll("[data-job]").forEach((button) => {
    button.addEventListener("click", guard(() => selectJob(button.dataset.job)));
  });
  list.querySelectorAll("[data-job-menu]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleJobMenu(button);
    });
  });
}

/* ---- The row menu. One open at a time, closed by anything else. ---- */
function closeJobMenu() {
  document.querySelectorAll(".job-menu-panel").forEach((panel) => panel.remove());
  document.querySelectorAll('.job-menu[aria-expanded="true"]')
    .forEach((button) => button.setAttribute("aria-expanded", "false"));
}

function toggleJobMenu(button) {
  const wasOpen = button.getAttribute("aria-expanded") === "true";
  closeJobMenu();
  if (wasOpen) return;
  const jobId = button.dataset.jobMenu;
  const panel = document.createElement("div");
  panel.className = "job-menu-panel";
  panel.setAttribute("role", "menu");
  panel.innerHTML = `<button type="button" role="menuitem" class="is-danger" data-job-delete="${escapeHTML(jobId)}">Delete</button>`;
  panel.querySelector("[data-job-delete]").addEventListener("click", (event) => {
    event.stopPropagation();
    closeJobMenu();
    openDeleteCreationDialog(jobId);
  });
  button.parentElement.appendChild(panel);
  button.setAttribute("aria-expanded", "true");
  panel.querySelector("button").focus();
}

/* ---- My creations: the project's generations as a durable gallery ---- */
function renderCreationsGallery(jobs = projectJobs()) {
  const grid = $("creationsGrid");
  if (!grid) return;
  if (!jobs.length) {
    grid.className = "creations-grid empty-state";
    grid.innerHTML = `
      <div class="empty-block">
        <span class="empty-icon" aria-hidden="true">${ICON_FRAME}</span>
        <strong>Your gallery is empty</strong>
        <p>Everything you generate in this project lands here and stays across reloads, ready to save as an asset.</p>
        <div class="btn-row btn-row-center">
          <button class="btn btn-primary" type="button" data-empty-action="go-create">Generate your first frame</button>
        </div>
      </div>`;
    return;
  }
  grid.className = "creations-grid";
  grid.innerHTML = jobs.map((job) => {
    const tone = statusTone(job.status);
    const finished = job.status === "COMPLETED" && job.output_asset_id;
    const saved = state.confirmedAssets.has(job.output_asset_id);
    const when = job.created_at ? job.created_at.slice(0, 16).replace("T", " ") : job.id.slice(0, 8);
    return `<figure class="creation-card" data-creation="${escapeHTML(job.id)}">
      <div class="creation-thumb${finished ? "" : " is-pending"}" data-surface="dark"
           data-creation-thumb="${escapeHTML(job.output_asset_id || "")}">
        ${finished ? "" : `<span class="status-chip ${tone}">${simpleLabel(job.status)}</span>`}
      </div>
      <figcaption>
        <b>${escapeHTML(job.shotLabel || friendlyModel(job.model))}</b>
        <small class="mono">${escapeHTML(when)}</small>
      </figcaption>
      ${finished ? `<div class="creation-actions">
        <button class="btn btn-tertiary" type="button" data-creation-open="${escapeHTML(job.id)}">View</button>
        <button class="btn btn-tertiary" type="button" data-creation-save="${escapeHTML(job.id)}"${saved ? " disabled" : ""}>${saved ? "Saved" : "Save to project"}</button>
      </div>` : ""}
    </figure>`;
  }).join("");
  jobs.filter((job) => job.status === "COMPLETED" && job.output_asset_id).forEach(async (job) => {
    const url = await cachedAssetThumbnail(job.output_asset_id).catch(() => null);
    const cell = grid.querySelector(`[data-creation-thumb="${CSS.escape(job.output_asset_id)}"]`);
    if (url && cell) cell.innerHTML = `<img src="${escapeHTML(url)}" alt="" loading="lazy" />`;
  });
  grid.querySelectorAll("[data-creation-open]").forEach((button) => {
    button.addEventListener("click", guard(() => openCreationMedia(button.dataset.creationOpen)));
  });
  grid.querySelectorAll("[data-creation-save]").forEach((button) => {
    button.addEventListener("click", () => openSaveDialogForJob(button.dataset.creationSave));
  });
}

let mediaViewerObjectUrl = null;
async function openCreationMedia(jobId) {
  const job = state.jobs.get(jobId);
  if (!job?.output_asset_id) return;
  const media = await resolveAssetMedia(job.output_asset_id);
  if (!media) return toast("The media is not readable yet");
  if (mediaViewerObjectUrl) URL.revokeObjectURL(mediaViewerObjectUrl);
  mediaViewerObjectUrl = media.revocable ? media.url : null;
  $("mediaViewerBody").innerHTML = media.mime.startsWith("video/")
    ? `<video src="${escapeHTML(media.url)}" controls autoplay playsinline></video>`
    : `<img src="${escapeHTML(media.url)}" alt="Generated media" />`;
  if (!$("mediaViewerDialog").open) $("mediaViewerDialog").showModal();
}

function openSaveDialogForJob(jobId) {
  const job = state.jobs.get(jobId);
  if (!job?.output_asset_id) return toast("Wait for the generation to finish");
  state.savingJobId = jobId;
  $("promotePassengerAssetBtn").disabled = false;
  if (!$("saveAssetDialog").open) $("saveAssetDialog").showModal();
}

/* ---- Deleting a creation ----------------------------------------

   The one destructive action on this page. Two doors reach it — the row's
   menu and the Inspector — and both go through the same confirmation, the
   same request and the same local cleanup, so the four numbers on this page
   (the list, the state counts, the session panel and the project total) can
   never disagree about what is left.

   States with nothing left to stop delete in one step. Anything still being
   made is stopped first, server-side; the dialog says so in those words. */
const DIRECTLY_DELETABLE_JOB_STATES = new Set([
  "COMPLETED", "FAILED", "CANCELLED", "RETRY_WAIT", "WORKER_NEEDS_USER_ACTION",
]);

let pendingDeleteJobId = null;

function openDeleteCreationDialog(jobId) {
  const job = state.jobs.get(jobId);
  if (!job) return toast("Select a creation first");
  pendingDeleteJobId = jobId;
  const label = job.shotLabel || friendlyModel(job.model);
  const credits = jobCredits(job);
  $("deleteCreationSummary").innerHTML = `
    <strong>${escapeHTML(label)}</strong><br>
    <span class="status-chip ${statusTone(job.status)}">${simpleLabel(job.status)}</span><br>
    <span class="mono">${escapeHTML(job.id)}</span>${credits ? `<br>${credits} credits used` : ""}`;
  $("deleteCreationRunningNote").hidden = DIRECTLY_DELETABLE_JOB_STATES.has(job.status);
  $("deleteCreationError").textContent = "";
  $("confirmDeleteCreationBtn").disabled = false;
  if (!$("deleteCreationDialog").open) $("deleteCreationDialog").showModal();
}

function closeDeleteCreationDialog() {
  pendingDeleteJobId = null;
  $("deleteCreationDialog").close();
}

async function confirmDeleteCreation() {
  const jobId = pendingDeleteJobId;
  if (!jobId) return;
  const button = $("confirmDeleteCreationBtn");
  button.disabled = true;
  $("deleteCreationError").textContent = "";
  try {
    await deleteCreation(jobId);
  } catch (error) {
    $("deleteCreationError").textContent = error.message;
    button.disabled = false;
    return;
  }
  closeDeleteCreationDialog();
  toast("Deleted. Your credit history is unchanged.");
}

async function deleteCreation(jobId) {
  // The project is named on the request as well as resolved server-side, so a
  // stale row from another project can never delete something here.
  const projectId = state.jobs.get(jobId)?.project_id || state.project?.id || null;
  const scope = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  await request(`/v1/generations/${encodeURIComponent(jobId)}${scope}`, { method: "DELETE" });
  forgetJob(jobId);
}

/** Drop a creation from the session's memory so every count on this page
 *  falls immediately, without waiting for the next listing. */
function forgetJob(jobId) {
  state.jobs.delete(jobId);
  if (state.selectedJobId === jobId) state.selectedJobId = null;
  if ($("operationsJobId") && $("operationsJobId").value.trim() === jobId) {
    $("operationsJobId").value = "";
  }
  if (state.operations.job?.id === jobId) renderGenerationControl(null);
  renderProductions();
}

async function selectJob(id) {
  state.selectedJobId = id;
  $("operationsJobId").value = id;
  renderProductions();
  await loadGenerationJob();
}

/* ============================================================
   Admin — system health
   ============================================================ */
function jsonView(value) {
  return escapeHTML(JSON.stringify(value, null, 2));
}

function allProjectShots() {
  return state.episode?.scenes.flatMap((scene) => scene.shots) || [];
}

function syncOperationsContext() {
  if (!$("operationsShotSelect")) return;
  const shots = allProjectShots();
  const selectedShot = state.shot?.id || $("operationsShotSelect").value;
  $("operationsShotSelect").innerHTML = shots.length
    ? shots.map((shot, index) => `<option value="${escapeHTML(shot.id)}">Shot ${String(index + 1).padStart(2, "0")} · ${escapeHTML(shot.prompt || shot.user_prompt || "Untitled")}</option>`).join("")
    : '<option value="">Compile a script first</option>';
  if (shots.some((shot) => shot.id === selectedShot)) $("operationsShotSelect").value = selectedShot;

  const selectedCharacter = state.selectedCharacterId || $("operationsCharacterSelect").value;
  $("operationsCharacterSelect").innerHTML = state.characters.length
    ? state.characters.map((character) => `<option value="${escapeHTML(character.id)}">${escapeHTML(character.name)}</option>`).join("")
    : '<option value="">No characters</option>';
  if (state.characters.some((character) => character.id === selectedCharacter)) {
    $("operationsCharacterSelect").value = selectedCharacter;
  }

  const committed = state.candidates.filter((candidate) => candidate.status === "COMMITTED");
  $("narrativeCandidateSelect").innerHTML = committed.length
    ? committed.map((candidate, index) => `<option value="${escapeHTML(candidate.id)}">Variant ${String.fromCharCode(65 + index)} · ${escapeHTML(candidate.id.slice(0, 8))}</option>`).join("")
    : '<option value="">No committed candidate on this shot</option>';
}

/** Not configured is grey. Only a configured provider that fails its probe is red. */
function providerState(provider) {
  if (provider.configured === false) return { tone: "is-neutral", label: "Not configured" };
  if (provider.health?.ok) return { tone: "is-ok", label: "Live" };
  return { tone: "is-danger", label: "Unhealthy" };
}

function renderProviders() {
  if (!$("providerHealthGrid")) return;
  const providers = state.operations.providers;
  const configured = providers.filter((provider) => provider.configured !== false);
  const models = configured.flatMap((provider) => provider.models || []).filter((model) => model.status !== "disabled");
  setText("operationsProviderMetric", `${configured.length} / ${providers.length}`);
  setText("operationsModelMetric", String(models.length));
  setText("operationsSkillMetric", String(state.operations.skills.length));

  const grid = $("providerHealthGrid");
  if (!providers.length) {
    grid.className = "provider-grid empty-state";
    grid.textContent = "No providers registered.";
    return;
  }
  grid.className = "provider-grid";
  grid.innerHTML = providers.map((provider) => {
    const { tone, label } = providerState(provider);
    const available = (provider.models || []).filter((model) => model.status !== "disabled").length;
    const latency = (provider.models || []).map((model) => model.latency).find(Boolean);
    const capabilities = [...new Set((provider.models || []).flatMap((model) => model.supported_operations || []))];
    return `<article class="provider">
      <div class="provider-top">
        <strong>${escapeHTML(simpleLabel(provider.name))}</strong>
        <span class="status-chip ${tone}">${label}</span>
      </div>
      <div class="provider-facts">
        <span>Models <b>${available}</b></span>
        ${latency ? `<span>Latency <b>${escapeHTML(String(latency))}</b></span>` : ""}
        ${capabilities.length ? `<span>Ops <b>${escapeHTML(String(capabilities.length))}</b></span>` : ""}
      </div>
      <p class="provider-detail">${escapeHTML(provider.health?.detail || provider.detail || (provider.configured === false ? "No credentials configured. Excluded from routing." : "No detail reported."))}</p>
    </article>`;
  }).join("");

  const catalog = $("skillCatalog");
  if (!state.operations.skills.length) {
    catalog.className = "skill-catalog empty-state";
    catalog.textContent = "No skills registered.";
    return;
  }
  catalog.className = "skill-catalog";
  catalog.innerHTML = state.operations.skills.map((skill) => `
    <div class="skill-item"><strong>${escapeHTML(skill.name)} · ${escapeHTML(skill.version)}</strong><small>${escapeHTML(skill.category)} · ${escapeHTML(skill.description)}</small></div>`).join("");
}

async function loadOperations() {
  if (!$("providerHealthGrid")) return;
  const [providers, skills] = await Promise.all([request("/v1/providers"), request("/v1/skills")]);
  const health = await Promise.all(providers.map((provider) => request(`/v1/providers/${encodeURIComponent(provider.name)}/health`)
    .catch((error) => ({ ok: false, detail: error.message }))));
  state.operations.providers = providers.map((provider, index) => ({ ...provider, health: health[index] }));
  state.operations.skills = skills;
  renderProviders();
  syncOperationsContext();
}

function selectedJobId() {
  return $("operationsJobId").value.trim();
}

/** What happened, in order, for a creator. The raw event codes and their JSON
 *  payloads are provider telemetry: the admin console keeps them, this
 *  inspector does not. */
const EVENT_LABELS = {
  REQUEST_SUBMITTED: "Sent to the model",
  WORKER_SELECTED: "Model chosen",
  PROVIDER_JOB_POLL: "Checking progress",
  MEDIA_DOWNLOADED: "Result downloaded",
  VIDEO_GENERATED: "Video finished",
  JOB_COMPLETED: "Done",
};

function renderGenerationControl(job) {
  state.operations.job = job;
  if (!job) {
    setText("operationsJobMetric", "None");
    $("generationControlStatus").className = "output-box empty-state";
    $("generationControlStatus").innerHTML = `
      <p>Select a creation, or paste a creation ID on the left.</p>
      <div class="btn-row">
        <button class="btn btn-tertiary" type="button" data-empty-action="go-productions">Open Productions</button>
      </div>`;
    ["retryJobBtn", "cancelJobBtn", "reconcileJobBtn", "deleteJobBtn"]
      .forEach((id) => { if ($(id)) $(id).disabled = true; });
    return;
  }
  rememberJob(job);
  setText("operationsJobMetric", simpleLabel(job.status));
  $("operationsJobId").value = job.id;
  // Credits are RESERVED until the job settles; saying "charged" while they
  // are still held is the one thing this line must never do.
  const held = ["RESERVED", "RECONCILIATION_REQUIRED"].includes(job.credit_status);
  const attempts = Number(job.attempt_count || 0);
  const credits = jobCredits(job);
  $("generationControlStatus").className = "output-box";
  $("generationControlStatus").innerHTML = `
    <span class="status-chip ${statusTone(job.status)}">${simpleLabel(job.status)}</span><br>
    ${escapeHTML(friendlyModel(job.model))}<br>
    ${credits} credits ${held ? "reserved" : "charged"}<br>
    Tried ${attempts} time${attempts === 1 ? "" : "s"}
    ${job.error_message ? `<br><span class="output-error">${escapeHTML(job.error_message)}</span>` : ""}`;
  $("retryJobBtn").disabled = job.safe_to_retry !== true;
  $("cancelJobBtn").disabled = !["QUEUED", "SUBMITTED", "RUNNING", "RETRY_WAIT"].includes(job.status);
  $("reconcileJobBtn").disabled = !["SENT_UNCONFIRMED", "SUBMITTED"].includes(job.submission_state) && !["FAILED", "RUNNING"].includes(job.status);
  // Any creation can be deleted; one still being made is stopped first.
  if ($("deleteJobBtn")) $("deleteJobBtn").disabled = false;

  const events = job.events || [];
  const list = $("generationEvents");
  list.className = events.length ? "event-list" : "event-list empty-state";
  list.innerHTML = events.length
    ? events.map((event) => `<div class="event-item"><strong>${escapeHTML(EVENT_LABELS[event.type] || sentenceCase(humanizeCode(event.type)))}</strong><small>${escapeHTML(event.created_at || "")}</small></div>`).join("")
    : "Nothing has happened on this creation yet";
  renderProductions();
}

async function loadGenerationJob() {
  const id = selectedJobId();
  if (!id) return toast("Paste a creation ID");
  renderGenerationControl(await request(`/v1/generations/${encodeURIComponent(id)}`));
}

async function mutateGenerationJob(action) {
  const id = selectedJobId();
  if (!id) return toast("Load a creation first");
  await request(`/v1/generations/${encodeURIComponent(id)}/${action}`, { method: "POST", body: "{}" });
  await loadGenerationJob();
  await loadCredits();
  toast(({
    retry: "Trying again. The same submission is reused, so you are not charged twice.",
    cancel: "Cancelled. Your balance updates once the reserved credits are released.",
    reconcile: "Rechecked. The credit status now matches what actually happened.",
  })[action]);
}

async function loadShotAudit() {
  if (!$("operationsShotSelect")) return;
  const shotId = $("operationsShotSelect").value;
  if (!shotId) return toast("Select a shot first");
  const [cost, decisions, candidates] = await Promise.all([
    request(`/v1/shots/${shotId}/cost`).catch(() => null),
    request(`/v1/shots/${shotId}/decisions`),
    request(`/v1/shots/${shotId}/candidates`),
  ]);
  const transitionsByCandidate = await Promise.all(candidates.map((candidate) =>
    request(`/v1/shots/${shotId}/candidates/${candidate.id}/state-transitions`).catch(() => [])));
  const transitions = transitionsByCandidate.flat();

  $("shotCostView").className = cost ? "output-box" : "output-box empty-state";
  $("shotCostView").innerHTML = cost ? `<pre>${jsonView(cost)}</pre>` : "No cost recorded for this shot yet";
  $("shotDecisionList").className = decisions.length ? "event-list" : "event-list empty-state";
  $("shotDecisionList").innerHTML = decisions.length
    ? decisions.map((decision) => `<div class="event-item"><strong>${escapeHTML(simpleLabel(decision.decision_type))} → ${escapeHTML(simpleLabel(decision.selected_action))}</strong><small>${escapeHTML(decision.created_at || "")} · policy ${escapeHTML(decision.policy_version || "—")}</small><div>${jsonView({ reasons: decision.reason_codes, input: decision.input_features })}</div></div>`).join("")
    : "No policy decisions on this shot yet";
  $("stateTransitionList").className = transitions.length ? "event-list" : "event-list empty-state";
  $("stateTransitionList").innerHTML = transitions.length
    ? transitions.map((transition) => `<div class="event-item"><strong>v${escapeHTML(transition.base_state_version_id || "initial")} → v${escapeHTML(transition.target_version || "pending")}</strong><small>${escapeHTML(transition.timeline_scope_key || "main")} · ${escapeHTML(transition.commit_status || transition.status || "")}</small><div>${jsonView({ changed_paths: transition.changed_paths, patch: transition.patch, validations: transition.validations })}</div></div>`).join("")
    : "No character state transitions on this shot yet";
}

async function loadNarrativeState() {
  if (!$("narrativeStateView")) return;
  if (!state.project) return toast("Create a project first");
  const characterId = $("operationsCharacterSelect").value;
  if (!characterId) return toast("Select a character first");
  const scope = $("narrativeScope").value.trim() || "main";
  try {
    const result = await request(`/v1/projects/${state.project.id}/characters/${characterId}/narrative-state?timeline_scope_key=${encodeURIComponent(scope)}`);
    $("narrativeStateView").className = "output-box";
    $("narrativeStateView").innerHTML = `<strong>Version v${escapeHTML(result.narrative_state?.version || result.version || "—")}</strong><pre>${jsonView(result)}</pre>`;
  } catch (error) {
    $("narrativeStateView").className = "output-box empty-state";
    $("narrativeStateView").textContent = `Not initialized yet: ${error.message}`;
  }
}

async function initializeNarrativeState() {
  if (!$("narrativeStateInput")) return;
  if (!state.project || !state.shot) return toast("Select a project and a shot first");
  const characterId = $("operationsCharacterSelect").value;
  const candidateId = $("narrativeCandidateSelect").value;
  const reason = $("narrativeReason").value.trim();
  if (!characterId || !candidateId) return toast("Select a character and a committed candidate");
  if (!reason || !$("narrativeConfirm").checked) return toast("Give a reason and tick the explicit confirmation");
  let narrativeState;
  try { narrativeState = JSON.parse($("narrativeStateInput").value); }
  catch (_error) { return toast("State JSON is not valid"); }
  await request(`/v1/characters/${characterId}/narrative-state/initialize`, {
    method: "POST",
    body: JSON.stringify({
      project_id: state.project.id,
      shot_id: state.shot.id,
      candidate_id: candidateId,
      timeline_scope_key: $("narrativeScope").value.trim() || "main",
      narrative_state: narrativeState,
      reason,
      explicit_confirmation: true,
    }),
  });
  $("narrativeConfirm").checked = false;
  await loadNarrativeState();
  toast("Narrative state initialized with a confirmation record.");
}

async function sha256File(file) {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function directUploadAsset() {
  if (!$("directUploadFile")) return;
  if (!state.project) return toast("Create a project first");
  const file = $("directUploadFile").files[0];
  if (!file) return toast("Choose a file to upload");
  const button = $("directUploadBtn");
  button.disabled = true;
  button.textContent = "Hashing…";
  try {
    const digest = await sha256File(file);
    const scope = $("directUploadScope").value;
    const payload = {
      project_id: state.project.id,
      asset_type: $("directUploadType").value,
      filename: file.name,
      mime_type: file.type || "application/octet-stream",
      sha256: digest,
      size_bytes: file.size,
      ...(scope === "shot" && state.shot ? { shot_id: state.shot.id } : {}),
      ...(scope === "character" && $("operationsCharacterSelect").value ? { character_id: $("operationsCharacterSelect").value } : {}),
    };
    button.textContent = "Requesting authorization…";
    const authorization = await request("/v1/assets/uploads", {
      method: "POST",
      headers: { "Idempotency-Key": `web-upload-${digest}-${file.size}` },
      body: JSON.stringify(payload),
    });
    if (!authorization.existing_asset_id) {
      button.textContent = "Uploading to storage…";
      const upload = await fetch(authorization.url, {
        method: authorization.method || "PUT",
        headers: authorization.headers || {},
        body: file,
      });
      if (!upload.ok) throw new Error(`Object storage rejected the upload (${upload.status})`);
    }
    button.textContent = "Verifying…";
    const asset = await request(`/v1/assets/uploads/${authorization.upload_id}/complete`, { method: "POST", body: "{}" });
    $("directUploadStatus").className = "output-box";
    $("directUploadStatus").innerHTML = `<strong>${asset.reused ? "Reused identical content" : "Upload complete"}</strong><br>Asset ${escapeHTML(asset.id)}<br>SHA-256 ${escapeHTML(asset.sha256)}<br>${escapeHTML(asset.storage_key)}`;
    $("directUploadFile").value = "";
    toast("File verified and registered as a project asset.");
  } catch (error) {
    const message = error instanceof TypeError && error.message === "Failed to fetch"
      ? "Object storage refused the browser upload. Allow this web origin, PUT and the presigned headers in the bucket's CORS rules."
      : error.message;
    $("directUploadStatus").className = "output-box";
    $("directUploadStatus").innerHTML = `<strong>Upload did not complete</strong><br>${escapeHTML(message)}`;
    throw new Error(message);
  } finally {
    button.disabled = false;
    button.textContent = "Verify and upload";
  }
}

/* ============================================================
   Dialogs
   ============================================================ */
let newProjectReturnFocus = null;
let projectCreationPending = false;

function openNewProjectDialog() {
  const dialog = $("newProjectDialog");
  newProjectReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : $("newProjectBtn");
  $("newProjectName").value = "Vertical short drama";
  $("newProjectStatus").textContent = "";
  $("newProjectError").textContent = "";
  $("newProjectName").setAttribute("aria-invalid", "false");
  $("cancelNewProjectBtn").disabled = false;
  $("confirmNewProjectBtn").disabled = false;
  $("confirmNewProjectBtn").textContent = "Create project";
  if (!dialog.open) dialog.showModal();
  requestAnimationFrame(() => { $("newProjectName").focus(); $("newProjectName").select(); });
}

function closeNewProjectDialog() {
  const dialog = $("newProjectDialog");
  if (!dialog.open || projectCreationPending) return;
  dialog.close();
}

async function createProject(name) {
  const project = await request("/v1/projects", { method: "POST", body: JSON.stringify({ title: name }) });
  await loadProjects();
  await selectProject(project.id);
  toast("Project created");
}

async function submitNewProject(event) {
  event.preventDefault();
  if (projectCreationPending) return;
  const name = $("newProjectName").value.trim();
  $("newProjectError").textContent = "";
  if (!name) {
    $("newProjectError").textContent = "Give the project a name";
    $("newProjectName").setAttribute("aria-invalid", "true");
    $("newProjectName").focus();
    return;
  }
  projectCreationPending = true;
  $("newProjectStatus").textContent = "Creating and switching…";
  $("cancelNewProjectBtn").disabled = true;
  $("confirmNewProjectBtn").disabled = true;
  $("confirmNewProjectBtn").textContent = "Creating…";
  try {
    await createProject(name);
    projectCreationPending = false;
    $("newProjectDialog").close();
  } catch (error) {
    $("newProjectStatus").textContent = "";
    $("newProjectError").textContent = error.message || "Could not create the project";
  } finally {
    projectCreationPending = false;
    $("cancelNewProjectBtn").disabled = false;
    $("confirmNewProjectBtn").disabled = false;
    $("confirmNewProjectBtn").textContent = "Create project";
  }
}

function openPasswordResetDialog() {
  $("resetEmail").value = $("authEmail").value.trim();
  $("resetToken").value = "";
  $("resetNewPassword").value = "";
  $("passwordResetStatus").textContent = "";
  $("passwordResetError").textContent = "";
  if (!$("passwordResetDialog").open) $("passwordResetDialog").showModal();
}

async function requestPasswordReset() {
  const email = $("resetEmail").value.trim();
  if (!email) return;
  $("passwordResetError").textContent = "";
  const result = await request("/api/auth/password-reset/request", { method: "POST", body: JSON.stringify({ email }) });
  $("passwordResetStatus").textContent = result.message;
  if (result.reset_token) {
    $("resetToken").value = result.reset_token;
    $("passwordResetStatus").textContent += " A token was filled in for you.";
  }
}

async function confirmPasswordReset(event) {
  event.preventDefault();
  $("passwordResetError").textContent = "";
  try {
    const result = await request("/api/auth/password-reset/confirm", {
      method: "POST",
      body: JSON.stringify({ token: $("resetToken").value.trim(), new_password: $("resetNewPassword").value }),
    });
    $("passwordResetStatus").textContent = result.message;
    $("authEmail").value = $("resetEmail").value.trim();
    $("passwordResetDialog").close();
    toast("Password reset. Sign in with the new one.");
  } catch (error) {
    $("passwordResetError").textContent = error.message;
  }
}

/* ============================================================
   Create with BestShiny Director page
   ============================================================ */
const CREATIVE_STAGE_LABEL = {
  INTAKE: "Idea", CLARIFYING: "Clarifying", BRIEF_PROPOSED: "Brief proposed",
  BRIEF_APPROVED: "Brief approved", SCREENPLAY_PROPOSED: "Screenplay drafted",
  SCREENPLAY_APPROVED: "Screenplay approved", VISUALS_IN_PROGRESS: "Key visuals",
  BIBLE_PROPOSED: "Bible drafted", BIBLE_LOCKED: "Bible locked",
  BEATS_PROPOSED: "Beats proposed", COMPILED: "Shots built", ABANDONED: "Abandoned",
};
const CREATIVE_FORMATS = [
  "SHORT_DRAMA", "ADVERTISEMENT", "PRODUCT_SHOWCASE", "SOCIAL_SHORT", "MUSIC_VISUAL",
  "FASHION_LOOKBOOK", "BEAUTY_TUTORIAL", "CONCEPT_FILM",
];
const CREATIVE_ASPECTS = ["9:16", "16:9", "1:1", "4:3", "3:4", "21:9", "3:2", "2:3"];
const PROVENANCE_LABEL = {
  USER_STATED: ["you said", "is-user"], USER_EDIT: ["you edited", "is-user"],
  ASSUMPTION_ACCEPTED: ["accepted assumption", "is-user"],
  MODEL_INFERRED: ["director assumed", "is-assumed"], DEFAULT: ["default", "is-assumed"],
};
const QUESTION_LABEL = {
  UNASKED: "not asked yet", ASKED: "asked, unanswered", ANSWERED: "answered",
  SKIPPED_BY_USER: "skipped by you", ASSUMPTION_ACCEPTED: "assumption accepted",
};
const REASONER_LABEL = (reasoner) => {
  if (!reasoner) return "";
  if (reasoner === "DETERMINISTIC") return "rules engine (director model unavailable)";
  if (reasoner.startsWith("MODEL:")) return "director model";
  if (reasoner === "USER_EDIT") return "your edit";
  return reasoner.toLowerCase();
};
// Key visuals poll with backoff: 3s, 5s, 8s, 13s, 20s, then every 30s, for at most 15 minutes.
const CREATIVE_POLL_STEPS_MS = [3000, 5000, 8000, 13000, 20000, 30000];
const CREATIVE_POLL_BUDGET_MS = 15 * 60 * 1000;
let creativePoll = null;
let creativeReplyInFlight = false;

function creativeSessionId() { return state.creative.session?.session?.id || null; }

async function loadCreativeSessions() {
  if (!state.project) return;
  state.creative.sessions = await request(`/v1/creative/sessions?project_id=${encodeURIComponent(state.project.id)}`);
  $("creativeSessionCount").textContent = state.creative.sessions.length;
  const list = $("creativeSessionList");
  if (!state.creative.sessions.length) {
    list.className = "shot-tree empty-state";
    list.innerHTML = `
      <div class="empty-block is-compact">
        <strong>No director sessions yet</strong>
        <p>Start one above and it stays here, so you can pick the conversation up later.</p>
        <div class="btn-row btn-row-center">
          <button class="btn btn-tertiary" type="button">Start a session</button>
        </div>
      </div>`;
    // Bound by reference rather than through a data-* verb: the idea box is
    // right above this rail, so the move is "put the cursor there", not a
    // route change the dispatcher would have to learn.
    list.querySelector("button")?.addEventListener("click", () => $("creativeIdeaInput").focus());
    return;
  }
  list.className = "shot-tree";
  list.innerHTML = state.creative.sessions.map((session) => `
    <div class="tree-shot-row">
      <button class="tree-shot ${creativeSessionId() === session.id ? "active" : ""}" data-creative-session="${session.id}" type="button">
        <span class="tree-shot-label">${escapeHTML(session.title || "Untitled")}</span>
        <span class="badge">${escapeHTML(CREATIVE_STAGE_LABEL[session.status] || session.status)}</span>
      </button>
      ${session.status === "COMPILED" ? "" : `<button class="tree-shot-delete" type="button" data-creative-delete="${session.id}" title="Delete this conversation" aria-label="Delete this conversation">&times;</button>`}
    </div>`).join("");
}

async function deleteCreativeSession(id) {
  const session = state.creative.sessions.find((item) => item.id === id);
  const title = session?.title || "this conversation";
  if (!window.confirm(`Delete "${title}"? Its turns and generated visuals stay on record, but it leaves your list.`)) return;
  await request(`/v1/creative/sessions/${id}`, { method: "DELETE" });
  if (creativeSessionId() === id) {
    stopCreativePolling();
    state.creative.session = null;
    state.creative.beatEdits = {};
    renderCreative();
  }
  await loadCreativeSessions();
  toast("Conversation deleted");
}

async function openCreativeSession(id) {
  if (creativeSessionId() !== id) {
    // A different session: forget in-progress edits and any running poll, and
    // do not replay the typewriter over a conversation that already happened.
    stopCreativePolling();
    state.creative.beatEdits = {};
    state.creative.editingBrief = false;
    state.creative.editingScreenplay = false;
    state.creative.revealedTurn = null;
    state.creative.revealedScreenplay = null;
  }
  state.creative.session = await request(`/v1/creative/sessions/${id}`);
  if (state.creative.revealedTurn === null) {
    // First paint of a session: everything before now is history.
    const lastDirector = [...(state.creative.session.turns || [])].reverse().find((turn) => turn.speaker === "DIRECTOR");
    state.creative.revealedTurn = lastDirector ? lastDirector.sequence : 0;
    state.creative.revealedScreenplay = state.creative.session.screenplay?.id || null;
  }
  renderCreative();
  await loadCreativeSessions();
  syncCreativePolling();
}

async function startCreativeSession() {
  if (!state.project) { toast("Create a project first"); return; }
  const idea = $("creativeIdeaInput").value.trim();
  if (!idea) { toast("Tell the director what you want to make"); return; }
  const button = $("creativeStartBtn");
  button.disabled = true;
  button.textContent = "The director is thinking…";
  let started;
  try {
    started = await request("/v1/creative/sessions", {
      method: "POST",
      body: JSON.stringify({ project_id: state.project.id, idea, client_turn_id: newClientTurnId() }),
    });
  } finally {
    button.disabled = false;
    button.textContent = "Start with BestShiny Director";
  }
  $("creativeIdeaInput").value = "";
  reportDirectorReply(started);
  state.creative.revealedTurn = 0; // reveal the director's first words as they arrive
  state.creative.revealedScreenplay = null;
  await openCreativeSession(started.session_id);
}

function newClientTurnId() {
  return (window.crypto?.randomUUID?.() || `turn-${Date.now()}-${Math.random().toString(16).slice(2)}`);
}

function reportDirectorReply(reply) {
  if (!reply) return;
  if (reply.reasoner === "DETERMINISTIC") {
    const codes = (reply.reason_codes || []).filter((code) => code !== "SKILL_LOADED").join(", ");
    toast(`The director model was unavailable (${codes}); the rules engine answered.${reply.retryable ? " You can send again." : ""}`);
  } else if (reply.reasoner === "APPROVAL_BLOCKED") {
    toast(reply.message);
  }
}

async function sendCreativeReply() {
  const session = state.creative.session;
  if (!session || creativeReplyInFlight) return;
  const input = $("creativeReplyInput");
  const content = input.value.trim();
  if (!content) return;
  // One turn at a time, and one idempotency key per attempt: a retried send
  // replays the recorded reply instead of paying for a second one.
  creativeReplyInFlight = true;
  input.disabled = true;
  $("creativeReplyBtn").disabled = true;
  input.value = "";
  showCreativeThinking(content, "The director is thinking");
  try {
    const reply = await request(`/v1/creative/sessions/${session.session.id}/messages`, {
      method: "POST",
      body: JSON.stringify({ content, client_turn_id: newClientTurnId() }),
    });
    state.creative.thinking = null; // the reply is in; the real turns replace the placeholder
    reportDirectorReply(reply);
    await openCreativeSession(session.session.id);
  } catch (error) {
    input.value = content; // nothing landed; give the words back
    state.creative.thinking = null;
    renderCreative();
    throw error;
  } finally {
    creativeReplyInFlight = false;
    input.disabled = false;
    $("creativeReplyBtn").disabled = false;
    input.focus();
  }
}

/* ---- polling ------------------------------------------------------ */
function stopCreativePolling() {
  if (creativePoll) {
    window.clearTimeout(creativePoll.timer);
    creativePoll = null;
  }
}

function syncCreativePolling() {
  const view = state.creative.session;
  const generating = (view?.anchors || []).some((anchor) => anchor.status === "GENERATING");
  if (view && generating && state.page === "ai-director" && view.session.status === "VISUALS_IN_PROGRESS") {
    if (!creativePoll || creativePoll.sessionId !== view.session.id) startCreativePolling(view.session.id);
  } else {
    stopCreativePolling();
  }
}

function startCreativePolling(sessionId) {
  stopCreativePolling();
  const poll = { sessionId, attempt: 0, startedAt: Date.now(), timer: null };
  creativePoll = poll;
  const schedule = () => {
    const delay = CREATIVE_POLL_STEPS_MS[Math.min(poll.attempt, CREATIVE_POLL_STEPS_MS.length - 1)];
    poll.attempt += 1;
    poll.timer = window.setTimeout(tick, delay);
  };
  const tick = async () => {
    if (creativePoll !== poll) return;
    // Leaving the page or switching sessions ends the poll; a terminal state
    // or an unrecoverable error ends it below.
    if (state.page !== "ai-director" || creativeSessionId() !== sessionId) { stopCreativePolling(); return; }
    let synced;
    try {
      synced = await request(`/v1/creative/sessions/${sessionId}/visuals/sync`, { method: "POST", body: "{}" });
    } catch (error) {
      stopCreativePolling();
      toast(`Key visual sync stopped: ${error.message}`);
      return;
    }
    if (creativePoll !== poll) return;
    const view = await request(`/v1/creative/sessions/${sessionId}`).catch(() => null);
    if (creativePoll !== poll) return;
    if (view) { state.creative.session = view; renderCreative(); }
    const generating = (synced.anchors || []).some((anchor) => anchor.status === "GENERATING");
    if (!generating) {
      stopCreativePolling();
      renderCreative();
      if (synced.can_propose_bible) toast("Key visuals are ready. Draft the visual bible when you like.");
      else if (synced.failed) toast(`${synced.failed} key visual(s) failed. Retry them, or skip an optional one.`);
      return;
    }
    if (Date.now() - poll.startedAt > CREATIVE_POLL_BUDGET_MS) {
      stopCreativePolling();
      renderCreative();
      toast("Key visuals are taking a while; use Refresh visuals to keep checking.");
      return;
    }
    schedule();
  };
  schedule();
  renderCreative();
}

/* ---- rendering ---------------------------------------------------- */
function provChip(record) {
  if (!record) return "";
  const [label, cls] = PROVENANCE_LABEL[record.source] || [String(record.source || "").toLowerCase(), ""];
  return `<span class="prov-chip ${cls}" title="${escapeHTML(record.evidence || "")}">${escapeHTML(label)}</span>`;
}

/** Show the user's words at once and a thinking director underneath, before the server answers. */
function showCreativeThinking(userText, label) {
  const newest = [...(state.creative.session?.turns || [])].reverse().find((turn) => turn.speaker === "DIRECTOR");
  // The bubble lives until a director turn newer than the one on screen arrives.
  state.creative.thinking = { userText, label, sinceTurn: newest ? newest.sequence : 0 };
  renderCreative();
}

const TYPEWRITER_MIN_MS = 12;
const TYPEWRITER_MAX_MS = 28;
let typewriterToken = 0;

/** Reveal `text` into `node` a few characters at a time; a newer reveal cancels an older one. */
function typewrite(node, text, onDone) {
  const token = ++typewriterToken;
  node.textContent = "";
  node.classList.add("typing-caret");
  let index = 0;
  const step = () => {
    if (token !== typewriterToken || !node.isConnected) return;
    // Chunks of one to three characters read like tokens arriving, without taking a minute per paragraph.
    const chunk = text.length > 600 ? 4 : text.length > 240 ? 2 : 1;
    index = Math.min(text.length, index + chunk);
    node.textContent = text.slice(0, index);
    const turns = $("creativeTurns");
    if (turns) turns.scrollTop = turns.scrollHeight;
    if (index < text.length) {
      const pause = /[。！？.!?\n]/.test(text[index - 1]) ? TYPEWRITER_MAX_MS * 6 : TYPEWRITER_MIN_MS + Math.random() * (TYPEWRITER_MAX_MS - TYPEWRITER_MIN_MS);
      window.setTimeout(step, pause);
    } else {
      node.classList.remove("typing-caret");
      if (onDone) onDone();
    }
  };
  step();
}

/** Stagger the appearance of a container's direct children. */
function revealSequentially(container, stepMs = 140) {
  [...container.children].forEach((child, index) => {
    child.classList.add("reveal-item");
    child.style.setProperty("--reveal-delay", `${Math.min(index * stepMs, 4000)}ms`);
  });
}

function renderCreativeTurns(turns) {
  return turns.map((turn) => {
    const questions = (turn.questions || []).map((question) =>
      `<li>${escapeHTML(question.question)}</li>`).join("");
    const meta = [];
    if (turn.speaker === "DIRECTOR") {
      meta.push(REASONER_LABEL(turn.reasoner));
      const codes = (turn.reason_codes || []).filter((code) => !["SKILL_LOADED", "MODEL_REPLY", "MODEL_OPERATIONS_APPLIED"].includes(code));
      if (codes.length) meta.push(codes.join(", "));
      if (turn.context?.compressed) meta.push("earlier turns condensed");
    }
    const notes = (turn.result?.creative_notes || []).map((note) => `<li><i>${escapeHTML(note)}</i></li>`).join("");
    const pendingReveal = turn.speaker === "DIRECTOR" && state.creative.revealedTurn !== null && turn.sequence > state.creative.revealedTurn;
    return `<div class="creative-turn is-${turn.speaker.toLowerCase()}" data-turn-sequence="${turn.sequence}" ${pendingReveal ? 'data-reveal="pending"' : ""}>
      <b>${turn.speaker === "USER" ? "You" : "Director"}</b>
      <p data-turn-text>${pendingReveal ? "" : escapeHTML(turn.content)}</p>
      <div data-turn-extras ${pendingReveal ? "hidden" : ""}>
        ${questions ? `<ul>${questions}</ul>` : ""}
        ${notes ? `<ul>${notes}</ul>` : ""}
        ${meta.length ? `<small class="mono">${escapeHTML(meta.join(" · "))}</small>` : ""}
      </div>
    </div>`;
  }).join("") + renderThinkingBubble();
}

function renderThinkingBubble() {
  const thinking = state.creative.thinking;
  if (!thinking) return "";
  return `${thinking.userText ? `<div class="creative-turn is-user"><b>You</b><p>${escapeHTML(thinking.userText)}</p></div>` : ""}
    <div class="creative-turn is-director is-thinking"><b>Director</b>
      <p>${escapeHTML(thinking.label)} <span class="thinking-dots"><span></span><span></span><span></span></span></p></div>`;
}

/** After a render: type out the director turns that arrived since the last reveal. */
function animateNewDirectorTurns(view) {
  const pending = [...document.querySelectorAll('#creativeTurns [data-reveal="pending"]')];
  if (!pending.length) return;
  const turns = view.turns || [];
  const revealNext = (index) => {
    const node = pending[index];
    if (!node) return;
    const sequence = Number(node.dataset.turnSequence);
    const turn = turns.find((item) => item.sequence === sequence);
    if (!turn) return;
    const textNode = node.querySelector("[data-turn-text]");
    typewrite(textNode, turn.content, () => {
      const extras = node.querySelector("[data-turn-extras]");
      if (extras) extras.hidden = false;
      node.removeAttribute("data-reveal");
      state.creative.revealedTurn = Math.max(state.creative.revealedTurn || 0, sequence);
      const turnsBox = $("creativeTurns");
      if (turnsBox) turnsBox.scrollTop = turnsBox.scrollHeight;
      revealNext(index + 1);
    });
  };
  revealNext(0);
}

const CREATIVE_BRIEF_ROWS = [
  ["Format", "format", (fields) => fields.format],
  ["Core idea", "logline", (fields) => fields.logline],
  ["Duration", "duration_seconds", (fields) => fields.duration_seconds ? `${fields.duration_seconds}s` : null],
  ["Aspect", "aspect_ratio", (fields) => fields.aspect_ratio],
  ["Platform", "platform", (fields) => fields.platform],
  ["Look", "visual_style.medium", (fields) => fields.visual_style?.medium],
  ["Palette", "visual_style.palette", (fields) => fields.visual_style?.palette],
  ["Tone", "tone", (fields) => (fields.tone || []).join(", ") || null],
  ["Setting", "setting.location", (fields) => fields.setting?.location],
  ["Time", "setting.time", (fields) => fields.setting?.time],
  ["Product", "product.name", (fields) => fields.product?.name],
  ["Selling points", "product.selling_points", (fields) => (fields.product?.selling_points || []).join(", ") || null],
  ["Music", "music.mood", (fields) => fields.music?.mood],
  ["Hook", "hook", (fields) => fields.hook],
  ["Call to action", "call_to_action", (fields) => fields.call_to_action],
  ["Audience", "audience", (fields) => fields.audience],
];

const normalizeName = (value) => String(value || "").toLowerCase().split(/\s+/).filter(Boolean).join(" ");

function renderBriefFields(brief) {
  const fields = brief.fields || {};
  const provenance = brief.provenance || {};
  const rows = CREATIVE_BRIEF_ROWS
    .map(([label, path, read]) => [label, path, read(fields)])
    .filter(([, , value]) => value)
    .map(([label, path, value]) =>
      `<div class="kv"><span>${label}</span><b>${escapeHTML(String(value))}${provChip(provenance[path])}</b></div>`);
  const cast = (fields.characters || []).map((member) => {
    const detail = [member.role, member.look].filter(Boolean).join(" — ");
    return `<div class="kv"><span>Character</span><b>${escapeHTML(member.name)}${detail ? ` <small>${escapeHTML(detail)}</small>` : ""}${provChip(provenance[`characters/${normalizeName(member.name)}`])}</b></div>`;
  });
  return rows.concat(cast).join("") || "<p class='empty-inline'>Nothing captured yet.</p>";
}

// The pipeline's cast limit comes from the server (session.limits.max_cast,
// which is creative_director_core.schemas.MAX_CAST). The fallback matters only
// before the first state load; the number is never authored twice.
const CREATIVE_CAST_FALLBACK = 12;
function creativeMaxCast() {
  return Number(state.creative?.session?.session?.limits?.max_cast) || CREATIVE_CAST_FALLBACK;
}

function renderBriefEditor(brief) {
  const fields = brief.fields || {};
  const text = (label, path, value, extra = "") =>
    `<label${extra}>${label}<input data-brief-path="${path}" value="${escapeHTML(value ?? "")}" /></label>`;
  const select = (label, path, options, value) =>
    `<label>${label}<select data-brief-path="${path}"><option value="">—</option>${options.map((option) =>
      `<option value="${option}" ${option === value ? "selected" : ""}>${option}</option>`).join("")}</select></label>`;
  const cast = (fields.characters || []).map((member, index) => `
    <div class="creative-cast-row" data-cast-row="${index}">
      <input data-cast-field="name" placeholder="Name" value="${escapeHTML(member.name || "")}" />
      <input data-cast-field="role" placeholder="Role" value="${escapeHTML(member.role || "")}" />
      <input data-cast-field="look" placeholder="Look (wardrobe, hair, distinguishing marks)" value="${escapeHTML(member.look || "")}" />
      <button class="btn btn-tertiary" type="button" data-cast-remove="${index}" title="Remove">&times;</button>
    </div>`).join("");
  return [
    select("Format", "format", CREATIVE_FORMATS, fields.format),
    `<label class="span-2">Core idea<textarea data-brief-path="logline" rows="2">${escapeHTML(fields.logline || "")}</textarea></label>`,
    text("Duration (seconds)", "duration_seconds", fields.duration_seconds),
    select("Aspect", "aspect_ratio", CREATIVE_ASPECTS, fields.aspect_ratio),
    text("Platform", "platform", fields.platform),
    text("Look / medium", "visual_style.medium", fields.visual_style?.medium),
    text("Palette", "visual_style.palette", fields.visual_style?.palette),
    text("Tone (comma separated)", "tone", (fields.tone || []).join(", ")),
    text("Location", "setting.location", fields.setting?.location),
    text("Time of day", "setting.time", fields.setting?.time),
    text("Product", "product.name", fields.product?.name),
    text("Selling points (comma separated)", "product.selling_points", (fields.product?.selling_points || []).join(", ")),
    text("Music mood", "music.mood", fields.music?.mood),
    text("Hook", "hook", fields.hook),
    text("Call to action", "call_to_action", fields.call_to_action),
    text("Audience", "audience", fields.audience),
    `<div class="creative-cast"><b>Characters</b>${cast}<div><button class="btn btn-tertiary" type="button" data-cast-add="1" ${(fields.characters || []).length >= creativeMaxCast() ? "disabled" : ""}>Add character</button><small class="creative-cast-limit">Every character who appears on screen gets its own key visual and identity lock; at most ${creativeMaxCast()}.</small></div></div>`,
  ].join("");
}

function readPath(fields, path) {
  return path.split(".").reduce((node, part) => (node && typeof node === "object" ? node[part] : undefined), fields);
}

function collectBriefOperations(brief) {
  const fields = brief.fields || {};
  const editor = $("creativeBriefEditor");
  const operations = [];
  const listPaths = new Set(["tone", "product.selling_points"]);
  editor.querySelectorAll("[data-brief-path]").forEach((node) => {
    const path = node.dataset.briefPath;
    const raw = node.value.trim();
    const before = readPath(fields, path);
    let after = raw;
    if (listPaths.has(path)) after = raw ? raw.split(/[,，、;；/]+/).map((item) => item.trim()).filter(Boolean) : [];
    if (path === "duration_seconds") after = raw ? Number(raw) : null;
    const hadValue = Array.isArray(before) ? before.length > 0 : before !== undefined && before !== null && before !== "";
    const hasValue = Array.isArray(after) ? after.length > 0 : after !== null && after !== "";
    const same = JSON.stringify(Array.isArray(after) ? after : after) === JSON.stringify(before);
    if (!hadValue && hasValue) operations.push({ op: "SET", path, value: after, confidence: "USER_STATED", evidence: "brief editor" });
    else if (hadValue && !hasValue) operations.push({ op: "REMOVE", path, confidence: "USER_STATED", evidence: "brief editor" });
    else if (hadValue && hasValue && !same) operations.push({ op: "REPLACE", path, value: after, confidence: "USER_STATED", evidence: "brief editor" });
  });
  const beforeCast = new Map((fields.characters || []).map((member) => [normalizeName(member.name), member]));
  const seen = new Set();
  editor.querySelectorAll("[data-cast-row]").forEach((row) => {
    const member = {};
    row.querySelectorAll("[data-cast-field]").forEach((node) => { if (node.value.trim()) member[node.dataset.castField] = node.value.trim(); });
    if (!member.name) return;
    const key = normalizeName(member.name);
    seen.add(key);
    const previous = beforeCast.get(key);
    const changed = !previous || ["role", "look"].some((field) => (previous[field] || "") !== (member[field] || ""));
    if (changed) operations.push({ op: "UPSERT", path: "characters", value: member, confidence: "USER_STATED", evidence: "brief editor" });
  });
  beforeCast.forEach((member, key) => {
    if (!seen.has(key)) operations.push({ op: "REMOVE", path: "characters", value: { name: member.name }, confidence: "USER_STATED", evidence: "brief editor" });
  });
  return operations;
}

function renderQuestions(view) {
  const brief = view.brief;
  const status = view.session.status;
  const states = brief?.question_states || {};
  const blocking = new Map((brief?.blocking || []).map((item) => [item.code, item]));
  const lastQuestions = new Map();
  (view.turns || []).forEach((turn) => (turn.questions || []).forEach((question) => lastQuestions.set(question.code, question.question)));
  const editable = ["CLARIFYING", "BRIEF_PROPOSED"].includes(status);
  const rows = (brief?.completeness?.gaps || [])
    .filter((gap) => gap.weight >= 3 || blocking.has(gap.code))
    .map((gap) => {
      const state = states[gap.code] || {};
      const block = blocking.get(gap.code);
      const assumed = block?.assumed_value ?? state.assumed_value;
      const label = lastQuestions.get(gap.code) || gap.code.replace(/_/g, " ").toLowerCase();
      const buttons = [];
      if (editable && assumed !== undefined && assumed !== null && block) {
        buttons.push(`<button class="btn btn-secondary" type="button" data-question-accept="${gap.code}">Accept: ${escapeHTML(typeof assumed === "object" ? JSON.stringify(assumed) : String(assumed))}</button>`);
      }
      if (editable && !block && !["SKIPPED_BY_USER", "ASSUMPTION_ACCEPTED", "ANSWERED"].includes(state.status)) {
        buttons.push(`<button class="btn btn-tertiary" type="button" data-question-skip="${gap.code}">Skip</button>`);
      }
      return `<div class="creative-question">
        <span><b>${escapeHTML(label)}</b> · ${escapeHTML(QUESTION_LABEL[state.status] || "not asked yet")}${block ? ` <span class="prov-chip is-required">required</span>` : ""}</span>
        ${buttons.join("")}
      </div>`;
    });
  return rows.join("");
}

function renderAssumptions(brief) {
  const assumptions = brief?.assumptions || [];
  if (!assumptions.length) return "";
  const items = assumptions.map((item) => {
    const [label, cls] = PROVENANCE_LABEL[item.source] || [item.source, ""];
    const value = typeof item.value === "object" ? JSON.stringify(item.value) : String(item.value);
    return `<div class="creative-question"><span><b>${escapeHTML(item.path)}</b> = ${escapeHTML(value)} <span class="prov-chip ${cls}">${escapeHTML(label)}</span></span></div>`;
  });
  return `<div class="creative-notice is-warning"><b>Assumptions awaiting your confirmation</b>These values were not stated by you. Approving confirms them; edit the brief to change them.</div>${items.join("")}`;
}

function renderScreenplay(view) {
  const screenplay = view.screenplay;
  if (!screenplay) return "<p class='empty-inline'>The director has not written the screenplay yet.</p>";
  const content = screenplay.content || {};
  const treatment = content.treatment || {};
  const hook = treatment.hook || {};
  const list = (items) => (items || []).length ? `<ul>${items.map((item) => `<li>${escapeHTML(item)}</li>`).join("")}</ul>` : "<p class='empty-inline'>—</p>";
  const characters = (content.characters || []).map((character) => {
    const relationships = (character.relationships || []).map((rel) => `${rel.with}: ${rel.relation}`).join("; ");
    return `<li><b>${escapeHTML(character.name)}</b>${character.role ? ` (${escapeHTML(character.role)})` : ""}${character.look ? ` — ${escapeHTML(character.look)}` : ""}${character.wants ? ` · wants ${escapeHTML(character.wants)}` : ""}${relationships ? ` · ${escapeHTML(relationships)}` : ""}</li>`;
  }).join("");
  const scenes = (content.scenes || []).map((scene) =>
    `<li><b>${escapeHTML(scene.key)}</b>: ${escapeHTML(scene.location)} — ${escapeHTML(scene.time)}${scene.description ? ` · ${escapeHTML(scene.description)}` : ""}</li>`).join("");
  const beats = (content.beats || []).map((beat) => {
    const shots = (beat.shots || []).map((shot) => {
      const primary = shot.dialogue
        ? `<b>${escapeHTML(shot.dialogue.speaker)}:</b> ${escapeHTML(shot.dialogue.text)}`
        : `<b>${escapeHTML(shot.action.actor)}</b> ${escapeHTML(shot.action.verb.replace("_", " "))}${shot.action.object ? ` ${escapeHTML(shot.action.object)}` : ""}${shot.action.target ? ` → ${escapeHTML(shot.action.target)}` : ""}${shot.action.description ? ` <small>${escapeHTML(shot.action.description)}</small>` : ""}`;
      const states = [shot.start_state ? `start: ${shot.start_state}` : "", shot.end_state ? `end: ${shot.end_state}` : "", shot.gaze_target ? `gaze: ${shot.gaze_target}` : ""].filter(Boolean).join(" · ");
      const obligations = (shot.continuity_obligations || []).join("; ");
      return `<div class="creative-shot"><span class="mono">#${shot.sequence} ${escapeHTML(shot.shot_type)} ${shot.duration}s</span><div>${primary}${states ? `<small>${escapeHTML(states)}</small>` : ""}${obligations ? `<small>continuity: ${escapeHTML(obligations)}</small>` : ""}</div></div>`;
    }).join("");
    return `<div class="creative-beat"><b>${beat.sequence} · ${escapeHTML(beat.intent)}</b><p>${escapeHTML(beat.summary || "")}${beat.emotional_beat ? ` <i>(${escapeHTML(beat.emotional_beat)})</i>` : ""}</p><small>scene ${escapeHTML(beat.scene_key)} · ${(beat.characters || []).map(escapeHTML).join(", ")}</small>${shots}</div>`;
  }).join("");
  const claims = (content.product_claims || []).map((claim) => `${claim.claim}${claim.must_preserve ? " (must preserve)" : ""}`);
  const obligations = (content.obligations || []).map((item) => `${item.key}: ${item.promise} [${item.category}]`);
  const revisions = (view.screenplays || []).map((item) =>
    `r${item.revision} · ${item.status.toLowerCase()} · ${REASONER_LABEL(item.reasoner)}`).join(" | ");
  return `
    <h4>Treatment</h4>
    <p><b>${escapeHTML(treatment.title || "Untitled")}</b></p>
    <p data-premise="${escapeHTML(treatment.premise || "")}">${escapeHTML(treatment.premise || "")}</p>
    ${hook.opening_question ? `<p><b>Hook:</b> ${escapeHTML(hook.opening_question)}${hook.promise ? ` — ${escapeHTML(hook.promise)}` : ""}${hook.audience_feeling ? ` <i>(${escapeHTML(hook.audience_feeling)})</i>` : ""}</p>` : ""}
    ${treatment.audience_expectation ? `<p><b>Audience expects:</b> ${escapeHTML(treatment.audience_expectation)}</p>` : ""}
    ${treatment.visual_direction ? `<p><b>Visual direction:</b> ${escapeHTML(treatment.visual_direction)}</p>` : ""}
    ${treatment.tone_direction ? `<p><b>Tone:</b> ${escapeHTML(treatment.tone_direction)}</p>` : ""}
    ${treatment.ending ? `<p><b>Ending:</b> ${escapeHTML(treatment.ending)}</p>` : ""}
    <h4>Locked facts (invariants)</h4>${list(content.invariants)}
    <h4>Open to exploration (variables)</h4>${list(content.variables)}
    <h4>Characters &amp; relationships</h4><ul>${characters}</ul>
    <h4>Scenes</h4><ul>${scenes}</ul>
    <h4>Beats, dialogue &amp; shots</h4>${beats}
    ${claims.length ? `<h4>Product claims</h4>${list(claims)}` : ""}
    ${(content.required_copy || []).length ? `<h4>Copy that must survive</h4>${list(content.required_copy)}` : ""}
    ${obligations.length ? `<h4>Continuity obligations opened</h4>${list(obligations)}` : ""}
    ${(content.unresolved || []).length ? `<h4>Unresolved creative choices</h4>${list(content.unresolved)}` : ""}
    <h4>Script as compiled</h4><pre class="mono" style="white-space:pre-wrap;font-size:11px">${escapeHTML(screenplay.script_text || "")}</pre>
    <div class="creative-revisions">Revisions: ${escapeHTML(revisions)}${screenplay.skill_version ? ` · skill ${escapeHTML(screenplay.skill_version)}` : ""}</div>`;
}

function renderUncoveredElements(view) {
  const uncovered = view.session?.anchor_coverage?.uncovered || [];
  if (!uncovered.length) return "";
  const reasons = {
    NOT_IN_ANY_BEAT_OR_SHOT: "named in the treatment only, never on screen",
    SCENE_NOT_USED_BY_ANY_BEAT: "no beat plays in this scene",
    SCENE_ANCHOR_LIMIT: "beyond the scene key-visual budget",
    PROP_ANCHOR_LIMIT: "beyond the prop key-visual budget",
  };
  const rows = uncovered.map((item) =>
    `<li><b>${escapeHTML(item.title)}</b> <small>${escapeHTML(item.kind.toLowerCase())} · ${escapeHTML(reasons[item.reason] || item.reason)}</small></li>`).join("");
  return `<div class="creative-uncovered"><b>No key visual (on record)</b><ul>${rows}</ul></div>`;
}

function renderAnchors(view) {
  const anchors = view.anchors || [];
  const status = view.session.status;
  const actionable = ["VISUALS_IN_PROGRESS", "BIBLE_PROPOSED"].includes(status);
  // The key-visual action behind each anchor carries which attempt this is and
  // why the last one failed; the anchor row alone only knows a failure code.
  const attemptByAnchor = new Map();
  for (const action of view.actions || []) {
    if (action.kind !== "GENERATE_KEY_VISUAL") continue;
    const anchorId = action.payload?.anchor_id;
    if (anchorId) attemptByAnchor.set(anchorId, action);
  }
  return anchors.map((anchor) => {
    const buttons = [];
    if (actionable && anchor.status === "FAILED") {
      buttons.push(`<button class="btn btn-tertiary" type="button" data-anchor-retry="${anchor.id}">Retry</button>`);
      if (!anchor.required) buttons.push(`<button class="btn btn-tertiary" type="button" data-anchor-skip="${anchor.id}">Skip</button>`);
    }
    if (actionable && ["READY", "FAILED", "SKIPPED"].includes(anchor.status)) {
      // Before the lock the user decides what each character, scene or plate looks like.
      buttons.push(`<button class="btn btn-tertiary" type="button" data-anchor-regenerate="${anchor.id}" title="Ask for a different image, with your direction">Regenerate</button>`);
      buttons.push(`<button class="btn btn-tertiary" type="button" data-anchor-replace="${anchor.id}" title="Upload your own image for this visual">Use my image</button>`);
    }
    const thumbClass = anchor.status === "GENERATING" ? " is-generating" : anchor.status === "FAILED" ? " is-failed" : "";
    const thumbLabel = anchor.status === "READY" ? "…" : anchor.status === "GENERATING" ? "Rendering…" : escapeHTML(simpleLabel(anchor.status) || anchor.status);
    const action = attemptByAnchor.get(anchor.id);
    const attempt = Number(action?.result?.attempt || 0);
    const failure = action?.result?.error_message || action?.result?.error || anchor.failure_code;
    const detail = [
      anchor.kind.toLowerCase(), `v${anchor.version}`,
      anchor.required ? "required" : "optional",
      attempt > 1 ? `attempt ${attempt}` : "",
      anchor.status === "FAILED" && failure ? `failed: ${failure}` : "",
      anchor.skip_reason ? `skipped: ${anchor.skip_reason}` : "",
    ].filter(Boolean).join(" · ");
    return `
      <figure class="asset-card" data-anchor-asset="${anchor.media_asset_id || ""}">
        <div class="asset-thumb empty-state${thumbClass}" data-surface="dark" data-anchor-thumb="${anchor.id}">${thumbLabel}</div>
        <figcaption>
          <b>${escapeHTML(anchor.title)}</b>
          <small>${escapeHTML(detail)}</small>
          ${buttons.length ? `<div class="anchor-actions">${buttons.join("")}</div>` : ""}
        </figcaption>
      </figure>`;
  }).join("");
}

function renderBeats(view) {
  const beats = view.beats || [];
  const editable = view.session.status === "BEATS_PROPOSED";
  const edits = state.creative.beatEdits || {};
  if (!beats.length) return "<p class='empty-inline'>No beats drafted yet.</p>";
  return beats.map((beat) => {
    const shots = (beat.shots || []).map((shot, index) => {
      const edit = edits[beat.sequence]?.[index] || {};
      const primary = shot.dialogue !== null && shot.dialogue !== undefined
        ? `<b>${escapeHTML(shot.speaker || "")}:</b> ${escapeHTML(edit.dialogue ?? shot.dialogue)}`
        : `${escapeHTML(shot.action)}${(edit.description ?? shot.description) ? ` <small>${escapeHTML(edit.description ?? shot.description)}</small>` : ""}`;
      const editor = editable ? `<div class="creative-beat-edit">
          ${shot.dialogue !== null && shot.dialogue !== undefined
            ? `<input data-beat="${beat.sequence}" data-shot="${index}" data-shot-field="dialogue" value="${escapeHTML(edit.dialogue ?? shot.dialogue)}" placeholder="Line" />`
            : `<input data-beat="${beat.sequence}" data-shot="${index}" data-shot-field="description" value="${escapeHTML(edit.description ?? shot.description ?? "")}" placeholder="Staging note" />`}
          ${shot.dialogue !== null && shot.dialogue !== undefined ? "<span></span>" : `<select data-beat="${beat.sequence}" data-shot="${index}" data-shot-field="shot_type">${["WIDE", "MEDIUM", "CLOSE", "CLOSE_UP", "EXTREME_CLOSE_UP", "INSERT", "OVER_SHOULDER", "TWO_SHOT"].map((type) => `<option ${(edit.shot_type ?? shot.shot_type) === type ? "selected" : ""}>${type}</option>`).join("")}</select>`}
          <input type="number" min="1" max="15" step="0.5" data-beat="${beat.sequence}" data-shot="${index}" data-shot-field="duration" value="${escapeHTML(String(edit.duration ?? shot.duration))}" />
        </div>` : "";
      return `<div class="creative-shot"><span class="mono">#${index + 1} ${escapeHTML(edit.shot_type ?? shot.shot_type)} ${edit.duration ?? shot.duration}s</span><div>${primary}${shot.start_state ? `<small>${escapeHTML(`${shot.start_state} → ${shot.end_state || ""}`)}</small>` : ""}${editor}</div></div>`;
    }).join("");
    return `<div class="creative-beat">
        <b>${beat.sequence} · ${escapeHTML(beat.intent)}</b>
        <p>${escapeHTML(beat.summary || "")}</p>
        <small>${escapeHTML(beat.location || "")} · ${escapeHTML(beat.time || "")} · ${(beat.shots || []).length} shot(s)</small>
        ${shots}
      </div>`;
  }).join("");
}

function collectEditedBeats(view) {
  const edits = state.creative.beatEdits || {};
  return (view.beats || []).map((beat) => ({
    ...beat,
    shots: (beat.shots || []).map((shot, index) => {
      const edit = edits[beat.sequence]?.[index];
      if (!edit) return shot;
      const merged = { ...shot };
      if (edit.dialogue !== undefined && shot.dialogue !== null) merged.dialogue = edit.dialogue;
      if (edit.description !== undefined) merged.description = edit.description;
      if (edit.shot_type !== undefined) merged.shot_type = edit.shot_type;
      if (edit.duration !== undefined) merged.duration = Number(edit.duration);
      return merged;
    }),
  }));
}

function setNotice(id, html, kind = "") {
  const node = $(id);
  if (!node) return;
  node.hidden = !html;
  node.className = `creative-notice ${kind}`.trim();
  node.innerHTML = html || "";
}

function renderCreative() {
  const view = state.creative.session;
  const hasSession = Boolean(view);
  $("creativeEmpty").hidden = hasSession;
  $("creativeFlow").hidden = !hasSession;
  if (!hasSession) {
    $("creativeStageCrumb").textContent = "No session";
    $("creativeStageChip").textContent = "Idea";
    document.querySelectorAll("[data-creative-stage], [data-creative-meta]").forEach((node) => { node.textContent = "—"; });
    return;
  }
  const status = view.session.status;
  $("creativeStageCrumb").textContent = view.session.title || "Session";
  $("creativeStageChip").textContent = CREATIVE_STAGE_LABEL[status] || status;
  // The thinking bubble goes away as soon as a newer director turn exists (or
  // when nothing is in flight any more).
  if (state.creative.thinking) {
    const newest = [...(view.turns || [])].reverse().find((turn) => turn.speaker === "DIRECTOR");
    const since = state.creative.thinking.sinceTurn;
    if ((since !== undefined && newest && newest.sequence > since) || (!creativeReplyInFlight && !state.creative.drafting)) {
      state.creative.thinking = null;
    }
  }
  $("creativeTurns").innerHTML = renderCreativeTurns(view.turns);
  $("creativeTurns").scrollTop = $("creativeTurns").scrollHeight;
  animateNewDirectorTurns(view);
  $("creativeReplyRow").hidden = !["INTAKE", "CLARIFYING", "BRIEF_PROPOSED"].includes(status);

  // Brief card
  const brief = view.brief;
  const briefStage = ["INTAKE", "CLARIFYING", "BRIEF_PROPOSED"].includes(status);
  $("creativeBriefCard").hidden = !brief || status === "COMPILED";
  if (brief) {
    $("creativeBriefStatus").textContent = brief.status === "APPROVED" ? "Approved" : (status === "CLARIFYING" ? "Clarifying" : "Proposed");
    const editing = Boolean(state.creative.editingBrief) && briefStage;
    $("creativeBriefFields").hidden = editing;
    $("creativeBriefFields").innerHTML = renderBriefFields(brief);
    $("creativeBriefEditor").hidden = !editing;
    if (editing && !$("creativeBriefEditor").innerHTML) $("creativeBriefEditor").innerHTML = renderBriefEditor(brief);
    $("creativeQuestions").innerHTML = briefStage && !editing ? renderQuestions(view) : "";
    $("creativeAssumptions").innerHTML = briefStage && !editing ? renderAssumptions(brief) : "";
    const blocking = brief.blocking || [];
    if (briefStage && status === "CLARIFYING") {
      setNotice("creativeBriefNotice", `<b>Still clarifying</b>${blocking.length ? `Required before approval: ${escapeHTML(blocking.map((item) => item.code).join(", "))}. Answer the director, or accept its assumption where offered.` : "Answer the open questions above, or skip the optional ones."}`, "is-warning");
    } else if (briefStage) {
      setNotice("creativeBriefNotice", "");
    } else {
      setNotice("creativeBriefNotice", `<b>Approved</b>This brief is frozen; the director writes from it.`);
    }
    $("creativeEditBriefBtn").hidden = !briefStage || editing;
    $("creativeSaveBriefBtn").hidden = !editing;
    $("creativeCancelBriefBtn").hidden = !editing;
    const hasAssumptions = (brief.assumptions || []).length > 0;
    $("creativeAcceptAssumptionsLabel").hidden = !(status === "BRIEF_PROPOSED" && hasAssumptions && !editing);
    const approve = $("creativeApproveBriefBtn");
    approve.hidden = !briefStage || editing;
    // The backend refuses anyway; the button only says so up front.
    approve.disabled = status !== "BRIEF_PROPOSED" || blocking.length > 0;
    approve.title = status !== "BRIEF_PROPOSED" ? "The brief is still being clarified" : "";
  }

  // Screenplay card
  const screenplay = view.screenplay;
  const screenplayStage = ["BRIEF_APPROVED", "SCREENPLAY_PROPOSED"].includes(status);
  $("creativeScreenplayCard").hidden = !(screenplay || screenplayStage);
  if (screenplay || screenplayStage) {
    $("creativeScreenplayStatus").textContent = screenplay ? `r${screenplay.revision} · ${simpleLabel(screenplay.status) || screenplay.status}` : "Drafting";
    const editing = Boolean(state.creative.editingScreenplay) && status === "SCREENPLAY_PROPOSED";
    $("creativeScreenplayBody").hidden = editing;
    if (state.creative.drafting) {
      $("creativeScreenplayBody").innerHTML = `<div class="creative-turn is-director is-thinking"><b>Director</b><p>${escapeHTML(state.creative.drafting)} <span class="thinking-dots"><span></span><span></span><span></span></span></p></div>`;
    } else {
      $("creativeScreenplayBody").innerHTML = renderScreenplay(view);
      if (screenplay && screenplay.id !== state.creative.revealedScreenplay) {
        // A screenplay that just arrived unfolds: the premise is typed out, then every section and beat slides in.
        state.creative.revealedScreenplay = screenplay.id;
        const body = $("creativeScreenplayBody");
        revealSequentially(body, 120);
        const premise = body.querySelector("[data-premise]");
        if (premise) typewrite(premise, premise.dataset.premise || "");
      }
    }
    $("creativeScreenplayEditor").hidden = !editing;
    if (editing && !$("creativeScreenplayJson").value) {
      $("creativeScreenplayJson").value = JSON.stringify(screenplay?.content || {}, null, 2);
    }
    const conflicts = (screenplay?.brief_conformance || []).filter((item) => item.severity === "BLOCKING");
    const enrichments = (screenplay?.brief_conformance || []).filter((item) => item.severity !== "BLOCKING");
    if (conflicts.length) {
      const rows = conflicts.map((item) =>
        `<li><b>${escapeHTML(item.brief_path)}</b> — brief: <i>${escapeHTML(JSON.stringify(item.brief_value))}</i>; screenplay: <i>${escapeHTML(JSON.stringify(item.screenplay_value))}</i><br><small>${escapeHTML(item.reason)}</small></li>`).join("");
      setNotice("creativeScreenplayNotice", `<b>This screenplay contradicts your approved brief</b>Ask the director to redraft, or approve anyway to overrule your own brief.<ul>${rows}</ul>`, "is-error");
    } else if (screenplay?.deterministic) {
      const codes = (screenplay.reason_codes || []).filter((code) => !["SKILL_LOADED", "DETERMINISTIC_FALLBACK"].includes(code)).join(", ");
      setNotice("creativeScreenplayNotice", `<b>Deterministic scaffold — not the director's writing</b>The director model was unavailable (${escapeHTML(codes)}). Every line is a placeholder. Redraft with the director, or approve knowing this.`, "is-error");
    } else if (screenplay?.reasoner === "USER_EDIT") {
      setNotice("creativeScreenplayNotice", `<b>Your revision</b>This revision was edited by you from r${screenplay.parent_revision ?? "?"}.`);
    } else if (screenplay) {
      const unresolved = (screenplay.content?.unresolved || []).length;
      const enriched = enrichments.length
        ? `<b>The director went beyond the brief</b>${enrichments.length} point(s) depart from what the director itself assumed, not from anything you fixed: ${escapeHTML(enrichments.map((item) => item.brief_path).join(", "))}.`
        : "";
      setNotice("creativeScreenplayNotice", unresolved ? `<b>Unresolved choices</b>The director left ${unresolved} creative choice(s) open; see the list below.` : enriched);
    } else {
      setNotice("creativeScreenplayNotice", `<b>Drafting</b>The director is writing the treatment and screenplay from the approved brief.`, "is-warning");
    }
    $("creativeRedraftBtn").hidden = !screenplayStage;
    $("creativeEditScreenplayBtn").hidden = status !== "SCREENPLAY_PROPOSED" || editing;
    $("creativeSaveScreenplayBtn").hidden = !editing;
    $("creativeCancelScreenplayBtn").hidden = !editing;
    $("creativeAcceptDeterministicLabel").hidden = !(status === "SCREENPLAY_PROPOSED" && screenplay?.deterministic && !editing);
    $("creativeAcceptBriefViolationsLabel").hidden = !(status === "SCREENPLAY_PROPOSED" && conflicts.length && !editing);
    $("creativeApproveScreenplayBtn").hidden = status !== "SCREENPLAY_PROPOSED" || editing;
  }

  // Key visuals card
  const anchors = view.anchors || [];
  const visualsVisible = anchors.length > 0;
  $("creativeVisualsCard").hidden = !visualsVisible;
  if (visualsVisible) {
    const ready = anchors.filter((anchor) => anchor.status === "READY").length;
    const failed = anchors.filter((anchor) => anchor.status === "FAILED").length;
    const skipped = anchors.filter((anchor) => anchor.status === "SKIPPED").length;
    const generating = anchors.some((anchor) => anchor.status === "GENERATING");
    const polling = Boolean(creativePoll && creativePoll.sessionId === view.session.id);
    $("creativeVisualsStatus").textContent = `${ready}/${anchors.length} ready${failed ? `, ${failed} failed` : ""}${skipped ? `, ${skipped} skipped` : ""}${polling ? " · auto-refreshing" : ""}`;
    $("creativeAnchorGrid").innerHTML = renderAnchors(view) + renderUncoveredElements(view);
    anchors.filter((anchor) => anchor.media_asset_id).forEach(async (anchor) => {
      const media = await resolveAssetThumbnail(anchor.media_asset_id).catch(() => null);
      const cell = document.querySelector(`[data-anchor-thumb="${anchor.id}"]`);
      if (media && cell) {
        cell.classList.remove("empty-state");
        cell.innerHTML = `<img src="${escapeHTML(media.url)}" alt="" loading="lazy" />`;
      }
    });
    const requiredNotReady = anchors.filter((anchor) => anchor.required && anchor.status !== "READY");
    const optionalOpen = anchors.filter((anchor) => !anchor.required && !["READY", "SKIPPED"].includes(anchor.status));
    const canPropose = !requiredNotReady.length && !optionalOpen.length;
    if (status === "VISUALS_IN_PROGRESS") {
      if (generating) setNotice("creativeVisualsNotice", `<b>Generating</b>${polling ? "This page refreshes on its own while visuals render." : "Use Refresh visuals to check progress."}`);
      else if (requiredNotReady.length) setNotice("creativeVisualsNotice", `<b>Required visuals not ready</b>${escapeHTML(requiredNotReady.map((anchor) => `${anchor.title} (${anchor.status.toLowerCase()})`).join(", "))}. Retry them; required visuals cannot be skipped.`, "is-error");
      else if (optionalOpen.length) setNotice("creativeVisualsNotice", `<b>Optional visuals pending</b>Retry or skip: ${escapeHTML(optionalOpen.map((anchor) => anchor.title).join(", "))}.`, "is-warning");
      else setNotice("creativeVisualsNotice", `<b>Ready</b>Every required visual is ready. Draft the visual bible.`);
    } else {
      setNotice("creativeVisualsNotice", "");
    }
    $("creativeRetryVisualsBtn").hidden = !failed || !["VISUALS_IN_PROGRESS", "BIBLE_PROPOSED"].includes(status);
    $("creativeProposeBibleBtn").disabled = !canPropose || !["VISUALS_IN_PROGRESS", "BIBLE_PROPOSED"].includes(status);
  }

  // Bible card
  const bible = view.bible;
  $("creativeBibleCard").hidden = !bible;
  if (bible) {
    $("creativeBibleStatus").textContent = simpleLabel(bible.status) || bible.status;
    const content = bible.content || {};
    const lineage = bible.lineage || {};
    $("creativeBibleContent").innerHTML = [
      ["Version", `v${bible.version}`],
      ["Look", content.style?.medium || content.rules?.medium],
      ["Palette", content.rules?.palette],
      ["Visual direction", content.visual_direction],
      ["Aspect", content.aspect_ratio],
      ["Screenplay", content.screenplay_revision ? `r${content.screenplay_revision}` : null],
      ["Anchors", `${(content.anchors || []).filter((anchor) => anchor.media_asset_id).length} bound, ${(content.anchors || []).filter((anchor) => anchor.status === "SKIPPED").length} skipped`],
      ["Locked", bible.locked_at ? new Date(bible.locked_at).toLocaleString() : "not yet"],
    ].filter(([, value]) => value).map(([label, value]) =>
      `<div class="kv"><span>${label}</span><b>${escapeHTML(String(value))}</b></div>`).join("");
    const identities = Object.entries(lineage.identities || {});
    $("creativeBibleLineage").innerHTML = [
      ["Lock status", lineage.lock_status || "NOT_LOCKED"],
      ["Style lock", lineage.style_lock_id ? `${lineage.style_inherited ? "inherited from the project" : "created"} · ${lineage.style_lock_id.slice(0, 8)}` : null],
      ["Character identities", identities.length ? identities.map(([key, entry]) => `${key.replace("character:", "")} v${entry.identity_version}`).join(", ") : null],
    ].filter(([, value]) => value).map(([label, value]) =>
      `<div class="kv"><span>${label}</span><b>${escapeHTML(String(value))}</b></div>`).join("");
    if (lineage.lock_status === "FAILED") {
      setNotice("creativeBibleNotice", `<b>Lock failed — compilation is blocked</b>${escapeHTML(lineage.error || "unknown error")} (${escapeHTML(lineage.error_type || "")}). Fix the cause and approve again; completed identities are kept.`, "is-error");
    } else if (bible.status === "LOCKED") {
      setNotice("creativeBibleNotice", `<b>Locked</b>Identities and style are bound through the platform's own locks.`);
    } else {
      setNotice("creativeBibleNotice", "");
    }
    $("creativeLockBibleBtn").hidden = bible.status !== "DRAFT";
    $("creativeLockBibleBtn").textContent = lineage.lock_status === "FAILED" ? "Retry lock" : "Approve & lock this version";
  }

  // Beats card
  $("creativeBeatsCard").hidden = !["BIBLE_LOCKED", "BEATS_PROPOSED", "COMPILED"].includes(status);
  $("creativeProposeBeatsBtn").hidden = status !== "BIBLE_LOCKED";
  $("creativeApproveBeatsBtn").hidden = status !== "BEATS_PROPOSED";
  const beats = view.beats || [];
  $("creativeBeatsStatus").textContent = beats.length ? (simpleLabel(beats[0].status) || beats[0].status) : "Waiting";
  $("creativeBeatList").innerHTML = renderBeats(view);

  $("creativeGoDirectorBtn").hidden = status !== "COMPILED";

  // Inspector stage list
  const afterBrief = !["INTAKE", "CLARIFYING", "BRIEF_PROPOSED"].includes(status);
  const stages = {
    brief: afterBrief ? "Approved" : (brief ? (status === "BRIEF_PROPOSED" ? "Proposed" : "Clarifying") : "—"),
    screenplay: screenplay ? (screenplay.status === "APPROVED" ? `Approved r${screenplay.revision}` : `${screenplay.deterministic ? "Scaffold" : "Draft"} r${screenplay.revision}`) : "—",
    visuals: anchors.length ? `${anchors.filter((anchor) => anchor.status === "READY").length}/${anchors.length}` : "—",
    bible: bible ? (simpleLabel(bible.status) || bible.status) : "—",
    beats: status === "COMPILED" ? "Shots built" : (beats.length ? "Proposed" : "—"),
  };
  document.querySelectorAll("[data-creative-stage]").forEach((node) => {
    node.textContent = stages[node.dataset.creativeStage] || "—";
  });
  const lastDirector = [...(view.turns || [])].reverse().find((turn) => turn.speaker === "DIRECTOR");
  const openQuestions = Object.values(brief?.question_states || {}).filter((item) => ["ASKED", "SKIPPED_BY_USER"].includes(item.status)).length;
  const meta = {
    reasoner: lastDirector ? REASONER_LABEL(lastDirector.reasoner) : "—",
    skill: lastDirector?.skill_version || (lastDirector ? "not loaded" : "—"),
    open: brief ? String(openQuestions) : "—",
    assumptions: brief ? String((brief.assumptions || []).length) : "—",
  };
  document.querySelectorAll("[data-creative-meta]").forEach((node) => {
    node.textContent = meta[node.dataset.creativeMeta] || "—";
  });
}

/* ---- actions ------------------------------------------------------ */
async function creativeApproveBrief() {
  const view = state.creative.session;
  if (!view?.brief) return;
  const accept = $("creativeAcceptAssumptions")?.checked === true;
  state.creative.drafting = "The director is writing the treatment and screenplay";
  state.creative.thinking = { userText: "", label: "Brief approved. Writing the screenplay", sinceTurn: Number.MAX_SAFE_INTEGER };
  $("creativeScreenplayCard").hidden = false;
  renderCreative();
  let result;
  try {
    result = await request(`/v1/creative/sessions/${view.session.id}/brief/approve`, {
      method: "POST",
      body: JSON.stringify({ revision: view.brief.revision, accept_assumptions: accept }),
    });
  } finally {
    state.creative.drafting = null;
    state.creative.thinking = null;
  }
  if (result.screenplay?.deterministic) toast("Brief approved. The director model was unavailable, so a labelled scaffold was drafted — redraft or approve it knowingly.");
  else if (result.screenplay) toast("Brief approved. The director has drafted the treatment and screenplay.");
  else toast(`Brief approved. ${result.screenplay_error?.message || ""}`);
  await openCreativeSession(view.session.id);
}

function creativeToggleBriefEditor(open) {
  state.creative.editingBrief = open;
  $("creativeBriefEditor").innerHTML = "";
  renderCreative();
}

async function creativeSaveBrief() {
  const view = state.creative.session;
  if (!view?.brief) return;
  const operations = collectBriefOperations(view.brief);
  if (!operations.length) { creativeToggleBriefEditor(false); return; }
  const result = await request(`/v1/creative/sessions/${view.session.id}/brief/edit`, {
    method: "POST",
    body: JSON.stringify({ operations }),
  });
  const rejected = result.rejected || [];
  if (rejected.length) toast(`${rejected.length} change(s) were not accepted: ${rejected.map((item) => `${item.path} (${item.reason})`).join(", ")}`);
  else toast(`Brief updated (revision ${result.revision}).`);
  state.creative.editingBrief = false;
  $("creativeBriefEditor").innerHTML = "";
  await openCreativeSession(view.session.id);
}

async function creativeResolveQuestion(code, action) {
  const view = state.creative.session;
  if (!view) return;
  const result = await request(`/v1/creative/sessions/${view.session.id}/brief/questions`, {
    method: "POST",
    body: JSON.stringify({ code, action }),
  });
  toast(action === "SKIP" ? `Skipped ${code.toLowerCase()}; a default was assumed where one exists.` : `Assumption accepted for ${code.toLowerCase()} (revision ${result.revision}).`);
  await openCreativeSession(view.session.id);
}

async function creativeRedraftScreenplay() {
  const view = state.creative.session;
  if (!view) return;
  const notes = window.prompt("Anything the director should change in the rewrite? (leave empty to redraft as is)", "") ?? null;
  if (notes === null) return;
  state.creative.drafting = notes ? "The director is rewriting the screenplay with your notes" : "The director is redrafting the screenplay";
  renderCreative();
  let result;
  try {
    result = await request(`/v1/creative/sessions/${view.session.id}/screenplay/propose`, {
      method: "POST",
      body: JSON.stringify({ notes }),
    });
  } finally {
    state.creative.drafting = null;
  }
  toast(result.deterministic ? "The director model was unavailable; a labelled scaffold was drafted instead." : `Screenplay redrafted (revision ${result.revision}).`);
  await openCreativeSession(view.session.id);
}

function creativeToggleScreenplayEditor(open) {
  state.creative.editingScreenplay = open;
  $("creativeScreenplayJson").value = "";
  renderCreative();
}

async function creativeSaveScreenplay() {
  const view = state.creative.session;
  if (!view?.screenplay) return;
  let content;
  try {
    content = JSON.parse($("creativeScreenplayJson").value);
  } catch (error) {
    toast(`The screenplay is not valid JSON: ${error.message}`);
    return;
  }
  const result = await request(`/v1/creative/sessions/${view.session.id}/screenplay/edit`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
  toast(`Saved as screenplay revision ${result.revision}.`);
  state.creative.editingScreenplay = false;
  $("creativeScreenplayJson").value = "";
  await openCreativeSession(view.session.id);
}

async function creativeApproveScreenplay() {
  const view = state.creative.session;
  if (!view?.screenplay) return;
  const accept = $("creativeAcceptDeterministic")?.checked === true;
  const overrule = $("creativeAcceptBriefViolations")?.checked === true;
  const result = await request(`/v1/creative/sessions/${view.session.id}/screenplay/approve`, {
    method: "POST",
    body: JSON.stringify({
      revision: view.screenplay.revision,
      accept_deterministic: accept,
      accept_brief_violations: overrule,
    }),
  });
  const failed = (result.executions || []).filter((entry) => entry.status === "FAILED");
  if (!(result.executions || []).length) toast("Nothing to retry — refresh the visuals first.");
  else if (failed.length) toast(`${failed.length} key visual(s) could not start: ${failed[0].error}`);
  else toast(`Screenplay approved. ${(result.executions || []).length} key visual(s) are being generated.`);
  await openCreativeSession(view.session.id);
}

async function creativeSyncVisuals() {
  const view = state.creative.session;
  if (!view) return;
  await request(`/v1/creative/sessions/${view.session.id}/visuals/sync`, { method: "POST", body: "{}" });
  await openCreativeSession(view.session.id);
}

async function creativeRetryVisuals() {
  const view = state.creative.session;
  if (!view) return;
  const result = await request(`/v1/creative/sessions/${view.session.id}/visuals/execute`, { method: "POST", body: "{}" });
  const executions = result.executions || [];
  const failed = executions.filter((entry) => entry.status === "FAILED");
  if (!executions.length) toast("Nothing to retry — the failed visuals may already be running.");
  else if (failed.length) toast(`${failed.length} key visual(s) still could not start: ${failed[0].error}`);
  else toast(`Retrying ${executions.length} key visual(s).`);
  await openCreativeSession(view.session.id);
}

async function creativeSkipAnchor(anchorId) {
  const view = state.creative.session;
  if (!view) return;
  const anchor = (view.anchors || []).find((item) => item.id === anchorId);
  const reason = window.prompt(`Skip "${anchor?.title || "this visual"}"? Say why (recorded on the session):`, "") ?? null;
  if (reason === null) return;
  await request(`/v1/creative/sessions/${view.session.id}/visuals/anchors/${anchorId}/skip`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
  await openCreativeSession(view.session.id);
}

async function creativeRegenerateAnchor(anchorId) {
  const view = state.creative.session;
  if (!view) return;
  const anchor = (view.anchors || []).find((item) => item.id === anchorId);
  const direction = window.prompt(`How should "${anchor?.title || "this visual"}" look instead? Your direction is kept with the new version.`, "") ?? null;
  if (direction === null) return;
  const result = await request(`/v1/creative/sessions/${view.session.id}/visuals/anchors/${anchorId}/regenerate`, {
    method: "POST",
    body: JSON.stringify({ direction }),
  });
  const failed = (result.executions || []).filter((entry) => entry.status === "FAILED");
  if (failed.length) toast(`The new version could not start: ${failed[0].error}`);
  else toast(`Version ${result.anchor.version} of ${result.anchor.title} is being generated.`);
  await openCreativeSession(view.session.id);
}

const ANCHOR_UPLOAD_TYPE = { CHARACTER: "CHARACTER_REFERENCE", SCENE: "LOCATION_REFERENCE", PROP: "PROP_REFERENCE" };

function creativeReplaceAnchor(anchorId) {
  const view = state.creative.session;
  if (!view) return;
  const anchor = (view.anchors || []).find((item) => item.id === anchorId);
  if (!anchor) return;
  const picker = document.createElement("input");
  picker.type = "file";
  picker.accept = "image/*";
  picker.addEventListener("change", () => {
    const file = picker.files?.[0];
    if (!file) return;
    guard(async () => {
      toast(`Uploading your image for ${anchor.title}…`);
      const form = new FormData();
      form.append("project_id", state.project.id);
      form.append("asset_type", ANCHOR_UPLOAD_TYPE[anchor.kind] || "REFERENCE");
      form.append("file", file);
      if (anchor.character_id) form.append("character_id", anchor.character_id);
      const upload = await fetch(`${API}/v1/assets`, {
        method: "POST", body: form, credentials: "include", headers: csrfHeaders("POST"),
      });
      if (!upload.ok) {
        const detail = await upload.json().catch(() => ({}));
        throw new Error(typeof detail.detail === "string" ? detail.detail : "Image upload failed");
      }
      const media = await upload.json();
      const result = await request(`/v1/creative/sessions/${view.session.id}/visuals/anchors/${anchorId}/replace`, {
        method: "POST",
        body: JSON.stringify({ media_asset_id: media.id }),
      });
      toast(`${result.anchor.title} now uses your image (version ${result.anchor.version}).`);
      await openCreativeSession(view.session.id);
    })();
  });
  picker.click();
}

async function creativeProposeBible() {
  const view = state.creative.session;
  if (!view) return;
  await request(`/v1/creative/sessions/${view.session.id}/bible/propose`, { method: "POST", body: "{}" });
  await openCreativeSession(view.session.id);
}

async function creativeLockBible() {
  const view = state.creative.session;
  if (!view?.bible) return;
  const result = await request(`/v1/creative/sessions/${view.session.id}/bible/approve`, {
    method: "POST",
    body: JSON.stringify({ version: view.bible.version }),
  });
  const identities = Object.keys(result.lineage?.identities || {}).length;
  toast(`Visual bible locked: ${identities} character identit${identities === 1 ? "y" : "ies"} and the project style${result.lineage?.style_inherited ? " (inherited)" : ""} are bound.`);
  await openCreativeSession(view.session.id);
}

async function creativeProposeBeats() {
  const view = state.creative.session;
  if (!view) return;
  state.creative.beatEdits = {};
  await request(`/v1/creative/sessions/${view.session.id}/beats/propose`, { method: "POST", body: "{}" });
  await openCreativeSession(view.session.id);
}

async function creativeApproveBeats() {
  const view = state.creative.session;
  if (!view) return;
  const edited = collectEditedBeats(view);
  const result = await request(`/v1/creative/sessions/${view.session.id}/beats/approve`, {
    method: "POST",
    body: JSON.stringify({ plan_revision: view.session.beat_revision, beats: edited }),
  });
  state.creative.beatEdits = {};
  toast(`Built ${result.shot_ids.length} shots from screenplay revision ${result.screenplay_revision}. Continue in Director.`);
  await openCreativeSession(view.session.id);
  await selectProject(state.project.id);
  if (result.episode_id) await loadEpisode(result.episode_id);
}

async function creativeOpenInDirector() {
  const view = state.creative.session;
  switchPage("director");
  if (view?.session?.compiled_episode_id) await loadEpisode(view.session.compiled_episode_id);
}

function creativeRecordBeatEdit(node) {
  const beat = node.dataset.beat;
  const shot = Number(node.dataset.shot);
  const field = node.dataset.shotField;
  if (!beat || Number.isNaN(shot) || !field) return;
  const edits = state.creative.beatEdits || (state.creative.beatEdits = {});
  const beatEdits = edits[beat] || (edits[beat] = {});
  beatEdits[shot] = { ...(beatEdits[shot] || {}), [field]: node.value };
}

/* ============================================================
   Episodes strip + create next episode
   ============================================================ */
const CONTINUATION_NOTES = {
  CONTINUOUS: "Picks up exactly where the last shot ended — same place, and the previous tail frame may carry into the first shot.",
  TIME_JUMP: "Time moves. Story, characters and the locked look carry over; the old scene, lighting and tail frame do not.",
  LOCATION_CHANGE: "The place changes. Story, characters and the locked look carry over; the old scene, lighting and tail frame do not.",
};

async function loadEpisodeStrip() {
  if (!state.project) return;
  state.episodes = await request(`/v1/projects/${state.project.id}/episodes`);
  renderEpisodeStrip();
}

function renderEpisodeStrip() {
  const strip = $("episodeStrip");
  $("episodeCount").textContent = state.episodes.length;
  $("createNextEpisodeBtn").disabled = !state.episodes.length;
  if (!state.episodes.length) {
    strip.className = "episode-strip empty-state";
    strip.innerHTML = `
      <p class="empty-inline">No episodes yet</p>
      <button class="btn btn-tertiary" type="button">Create the first episode</button>`;
    strip.querySelector("button")?.addEventListener("click", () => {
      // #createNextEpisodeBtn continues an EXISTING episode and is disabled
      // while there are none, so with an empty strip the real first move is
      // the script box: an empty state that cannot be acted on is a label.
      const next = $("createNextEpisodeBtn");
      if (next && !next.disabled) next.click();
      else { switchPage("director"); $("scriptInput").focus(); }
    });
    return;
  }
  strip.className = "episode-strip";
  strip.innerHTML = state.episodes.map((episode) => `
    <button class="episode-chip ${state.episode?.id === episode.id ? "active" : ""}" data-episode-chip="${episode.id}" type="button" title="${escapeHTML(episode.title)}">
      <b>EP${String(episode.episode_number).padStart(2, "0")}</b>
      <span>${escapeHTML(simpleLabel(episode.display_status))}</span>
      <small class="mono">${episode.committed_shot_count}/${episode.shot_count}</small>
    </button>`).join("");
}

function setContinuationMode(mode) {
  state.continuation.mode = mode;
  document.querySelectorAll("[data-continuation-mode]").forEach((button) => {
    const active = button.dataset.continuationMode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active)); // role="tab"
  });
  $("continuationModeNote").textContent = CONTINUATION_NOTES[mode];
  $("continuationTimeGapField").hidden = mode === "CONTINUOUS";
  $("continuationLocationField").hidden = mode === "CONTINUOUS";
}

function openContinuationDialog() {
  if (!state.episodes.length) return;
  state.continuation.view = null;
  $("continuationPreview").hidden = true;
  $("continuationPreview").innerHTML = "";
  $("continuationError").textContent = "";
  $("confirmContinuationBtn").hidden = true;
  const last = state.episodes[state.episodes.length - 1];
  $("continuationSubtitle").textContent =
    `Continue after EP${String(last.episode_number).padStart(2, "0")} · ${last.title}. The story ledger, character state and locked look are inherited; what else carries depends on how it opens.`;
  setContinuationMode(state.continuation.mode);
  if (!$("continuationDialog").open) $("continuationDialog").showModal();
}

async function prepareContinuation() {
  const last = state.episodes[state.episodes.length - 1];
  if (!last) return;
  $("continuationError").textContent = "";
  try {
    state.continuation.view = await request(`/v1/episodes/${last.id}/continuations`, {
      method: "POST",
      body: JSON.stringify({
        continuation_mode: state.continuation.mode,
        time_gap: $("continuationTimeGap").value.trim(),
        new_location: $("continuationLocation").value.trim() || null,
        guidance: $("continuationGuidance").value.trim(),
        regenerate: Boolean(state.continuation.view),
      }),
    });
  } catch (error) {
    $("continuationError").textContent = error.message;
    return;
  }
  const view = state.continuation.view;
  const preview = $("continuationPreview");
  preview.hidden = false;
  preview.innerHTML = `
    <b>EP${String(view.next_episode_number).padStart(2, "0")} proposal · ${escapeHTML(simpleLabel(view.continuation_mode))}</b>
    <p>${escapeHTML(view.brief.premise || "")}</p>
    ${view.brief.carried_obligations?.length ? `<small>Carries: ${view.brief.carried_obligations.map(escapeHTML).join(" · ")}</small>` : ""}
    <ol>${view.beats.map((beat) => `<li><b>${escapeHTML(beat.intent)}</b> — ${escapeHTML(beat.summary || "")} <small>(${escapeHTML(beat.location || "")})</small></li>`).join("")}</ol>`;
  $("confirmContinuationBtn").hidden = false;
}

async function confirmContinuation() {
  const view = state.continuation.view;
  if (!view) return;
  $("continuationError").textContent = "";
  let result;
  try {
    result = await request(`/v1/continuations/${view.id}/confirm`, { method: "POST", body: "{}" });
  } catch (error) {
    $("continuationError").textContent = error.message;
    return;
  }
  $("continuationDialog").close();
  state.continuation.view = null;
  toast(`EP${String(result.next_episode_number).padStart(2, "0")} built: ${result.compiled.shot_count} shots inherit the series state.`);
  await selectProject(state.project.id);
  if (result.compiled?.episode_id) await loadEpisode(result.compiled.episode_id);
}

/* ============================================================
   Wiring
   ============================================================ */
document.querySelectorAll("[data-mode]").forEach((button) => {
  button.addEventListener("click", () => switchPage(button.dataset.mode));
});
document.querySelectorAll("[data-media]").forEach((button) => {
  button.addEventListener("click", () => setPassengerMedia(button.dataset.media));
});
document.querySelectorAll("[data-job-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    state.jobFilter = button.dataset.jobFilter;
    syncJobFilterTabs();
    renderProductions();
  });
});

/** The filter row is a role="tablist": the class carries the look, the
 *  aria-selected state carries the meaning. Both are set from one place so a
 *  filter changed from an empty state's "Show all" stays in sync. */
function syncJobFilterTabs() {
  document.querySelectorAll("[data-job-filter]").forEach((item) => {
    const active = item.dataset.jobFilter === state.jobFilter;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
  });
}

/* Empty states always offer the next move; this is where those moves land. */
document.addEventListener("click", (event) => {
  const action = event.target.closest("[data-empty-action]")?.dataset.emptyAction;
  if (!action) return;
  if (action === "new-project") openNewProjectDialog();
  if (action === "prompt") { switchPage("create"); $("passengerPrompt").focus(); }
  if (action === "upload") { switchPage("create"); $("passengerReference").click(); }
  if (action === "go-create") switchPage("create");
  if (action === "go-director") switchPage("director");
  // The value "compile" keeps its name even though the visible verb is now
  // "Break into shots": it is a dispatcher hook, not a label.
  if (action === "compile") { switchPage("director"); $("scriptInput").focus(); }
  if (action === "first-shot") {
    const first = document.querySelector("[data-shot]");
    if (first) first.click();
  }
  if (action === "generate") {
    const box = $("passengerPrompt");
    if (!box.value.trim()) { box.focus(); return toast("Describe the frame first — one sentence is enough."); }
    $("passengerGenerateBtn").click();
  }
  if (action === "example") {
    const box = $("passengerPrompt");
    box.value = box.placeholder.replace(/^e\.g\.\s*/, "");
    box.focus();
    box.dispatchEvent(new Event("input", { bubbles: true }));
  }
  if (action === "go-productions") switchPage("productions");
  if (action === "show-all-jobs") {
    state.jobFilter = "all";
    syncJobFilterTabs();
    renderProductions();
  }
  if (action === "generate-shot") {
    const button = $("generateBtn");
    // #candidateGrid ships this CTA before any shot is picked, and #generateBtn
    // stays disabled until one is. A disabled button dispatches no click event,
    // so without this branch the amber primary is silently dead on first paint.
    if (button.disabled) {
      const first = document.querySelector("[data-shot]");
      if (first) { first.click(); return toast("Opened the first shot — Generate shot is in the action bar."); }
      return toast("Break a script into shots first, then pick one to generate.");
    }
    button.click();
  }
});

/* The creation ID is a support handle, not something anyone should retype. */
document.addEventListener("click", (event) => {
  const id = event.target.closest("[data-copy-id]")?.dataset.copyId;
  if (!id) return;
  if (!navigator.clipboard) return toast("This browser will not let the page copy for you.");
  navigator.clipboard.writeText(id).then(
    () => toast("Creation ID copied"),
    () => toast("Could not copy the ID"),
  );
});

/** Cancelling from the canvas acts on the job the canvas is showing, not on
 *  whatever id the Productions inspector happens to hold. */
async function cancelPassengerJob(jobId) {
  await request(`/v1/generations/${encodeURIComponent(jobId)}/cancel`, { method: "POST", body: "{}" });
  await refreshPassengerJob();
  toast("Cancelled. Your balance updates once the reserved credits are released.");
}

document.addEventListener("click", (event) => {
  const cancelId = event.target.closest("[data-gen-cancel]")?.dataset.genCancel;
  if (cancelId) { guard(cancelPassengerJob)(cancelId); return; }
  // "Check again" after the ten-minute watch budget: a manual refresh clears
  // the stalled copy and restarts the poll, which is exactly what was asked.
  if (event.target.closest("[data-gen-resume]")) guard(refreshPassengerJob)();
});

/* Top bar */
on("newProjectBtn", "click", openNewProjectDialog);
on("projectSelect", "change", (event) => guard(selectProject)(event.target.value));
on("userMenuBtn", "click", (event) => {
  const panel = $("userMenu");
  const open = panel.hidden;
  panel.hidden = !open;
  event.currentTarget.setAttribute("aria-expanded", String(open));
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".user-menu")) $("userMenu").hidden = true;
});
on("adminNavBtn", "click", () => { $("userMenu").hidden = true; navigate("/admin"); });
on("assetsNavBtn", "click", () => { $("userMenu").hidden = true; guard(openAssetDetails)(null); });
on("logoutBtn", "click", guard(logout));

/* Auth */
on("authForm", "submit", submitAuth);
on("authModeBtn", "click", () => {
  const next = state.authMode === "login" ? "register" : "login";
  navigate(next === "register" ? "/signup" : "/login");
  setAuthMode(next);
});
on("forgotPasswordBtn", "click", openPasswordResetDialog);
on("passwordResetForm", "submit", confirmPasswordReset);
on("requestResetBtn", "click", () => requestPasswordReset().catch((error) => { $("passwordResetError").textContent = error.message; }));
on("closePasswordResetBtn", "click", () => $("passwordResetDialog").close());

/* Create */
on("correctImagePromptBtn", "click", guard(correctPassengerPrompt));
on("refinePromptBtn", "click", guard(refinePassengerPrompt));
on("undoImagePromptBtn", "click", undoPassengerPrompt);
on("passengerGenerateBtn", "click", guard(generatePassenger));
on("passengerRefreshBtn", "click", guard(refreshPassengerJob));
on("passengerModel", "change", updatePassengerCost);
on("passengerImageTier", "change", () => { syncImageTierHint(); updatePassengerCost(); });
on("passengerImageTask", "change", updatePassengerCost);
on("passengerAspect", "change", updatePassengerCost);
on("passengerDuration", "input", updatePassengerCost);
on("passengerResolution", "change", updatePassengerCost);
on("passengerReference", "change", (event) => {
  state.passengerReferenceUpload = null;
  const file = event.target.files[0];
  $("referenceFileName").hidden = !file;
  if (file) $("referenceFileName").textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} KB`;
  updatePassengerCost();
});
/* ---- Reference drag and drop ------------------------------------
   Two targets: the sidebar strip and the whole canvas. During a drag the
   files themselves are not readable, so the MIME check runs on the items
   list and the real check happens on drop. */
const dragCarriesImage = (transfer) => {
  if (!transfer) return false;
  const items = Array.from(transfer.items || []);
  if (items.length) return items.some((i) => i.kind === "file" && (!i.type || i.type.startsWith("image/")));
  return Array.from(transfer.types || []).includes("Files");
};

function acceptReferenceFile(file, target) {
  const reject = (message) => {
    target?.el.classList.add("is-reject");
    window.setTimeout(() => target?.el.classList.remove(target.cls, "is-reject"), 620);
    toast(message);
  };
  if (!file) return reject("Nothing landed — try dragging the file again.");
  if (!file.type.startsWith("image/")) return reject("That's not an image. Drop a PNG, JPG or WebP.");
  if (file.size > 20 * 1024 * 1024) return reject("That image is over 20 MB. Use a smaller file.");
  // Accepted: the drag is over and the highlight has done its job.
  target?.el.classList.remove(target.cls, "is-reject");
  const transfer = new DataTransfer();
  transfer.items.add(file);
  $("passengerReference").files = transfer.files;
  $("passengerReference").dispatchEvent(new Event("change"));
  toast(`Reference set — ${file.name}`);
}

[
  { el: document.querySelector(".dropzone"), cls: "is-over" },
  { el: $("passengerResult"), cls: "is-over" },
].filter((t) => t.el).forEach((target) => {
  let depth = 0;                                   // dragenter/dragleave fire per child crossed
  const clear = () => { depth = 0; target.el.classList.remove(target.cls, "is-reject"); };
  target.el.addEventListener("dragenter", (e) => {
    if (!dragCarriesImage(e.dataTransfer)) return;
    e.preventDefault(); depth += 1; target.el.classList.add(target.cls);
  });
  target.el.addEventListener("dragover", (e) => {
    if (!dragCarriesImage(e.dataTransfer)) return;
    e.preventDefault();                            // without this, `drop` never fires
    e.dataTransfer.dropEffect = "copy";            // the OS cursor finally agrees with the highlight
  });
  target.el.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) clear(); });
  target.el.addEventListener("drop", (e) => {
    e.preventDefault();
    // A drop ends the drag whatever the verdict, but a REJECTED drop never
    // reaches clear() — it only drops the classes on a 620 ms timer. Without
    // this reset the counter stays at 1, the next dragleave can never bring it
    // back to 0, and the whole-canvas overlay stays painted for good. The
    // classes are deliberately left alone here: `is-reject` is only visible
    // while `is-over` holds the overlay at opacity 1, so acceptReferenceFile
    // clears them itself once it knows the file was actually taken.
    depth = 0;
    acceptReferenceFile(e.dataTransfer?.files?.[0], target);
  });
});

// Anywhere else, a dropped file must NOT navigate the tab away from the SPA.
// Today it does, and the prompt, the selected shot, the in-flight poll and every
// object URL are lost with it.
["dragover", "drop"].forEach((type) => {
  window.addEventListener(type, (e) => {
    // The exception list is the two REAL drop targets plus file inputs. A
    // broad [data-surface="dark"] would also match the shot stage, the media
    // viewer and every creation thumb — none of which handle a drop, so a file
    // dropped there would still navigate the tab away.
    if (e.target.closest?.("#passengerResult, .dropzone, input[type=file]")) return;
    // Only a FILE drag navigates the tab away. Dragging text or a link into a
    // field is a native affordance of every input and textarea in the app and
    // this guard has no business killing it — a FILE dropped on a field does
    // still navigate, so that case stays cancelled.
    const carriesFiles = Array.from(e.dataTransfer?.types || []).includes("Files");
    if (!carriesFiles && e.target.closest?.("textarea, input, [contenteditable]")) return;
    e.preventDefault();
    if (type === "dragover" && e.dataTransfer) e.dataTransfer.dropEffect = "none";
  });
});
on("saveToProjectBtn", "click", () => {
  const job = state.passengerJobs[state.passengerMedia];
  if (!job?.output_asset_id) return toast("Wait for the generation to finish");
  state.savingJobId = null; // this flow saves the Create canvas's own job
  $("promotePassengerAssetBtn").disabled = false;
  if (!$("saveAssetDialog").open) $("saveAssetDialog").showModal();
});
on("closeSaveAssetBtn", "click", () => $("saveAssetDialog").close());
on("promotePassengerAssetBtn", "click", guard(confirmPassengerAsset));

/* Assets */
on("openAssetsBtn", "click", guard(() => openAssetDetails(null)));
on("closeAssetDetailsBtn", "click", () => $("assetDetailsDialog").close());
on("manualExistingAsset", "change", guard(syncManualAssetSelection));
on("manualAssetUploadBtn", "click", guard(uploadManualAssetVersion));
on("lockProjectStyleBtn", "click", guard(lockSelectedProjectStyle));

/* Create with BestShiny Director */
on("creativeStartBtn", "click", guard(startCreativeSession));
on("creativeReplyBtn", "click", guard(sendCreativeReply));
on("creativeReplyInput", "keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); guard(sendCreativeReply)(); }
});
on("creativeRefreshBtn", "click", guard(async () => {
  if (state.creative.session) await openCreativeSession(state.creative.session.session.id);
  else await loadCreativeSessions();
}));
on("creativeApproveBriefBtn", "click", guard(creativeApproveBrief));
on("creativeEditBriefBtn", "click", () => creativeToggleBriefEditor(true));
on("creativeCancelBriefBtn", "click", () => creativeToggleBriefEditor(false));
on("creativeSaveBriefBtn", "click", guard(creativeSaveBrief));
on("creativeRedraftBtn", "click", guard(creativeRedraftScreenplay));
on("creativeEditScreenplayBtn", "click", () => creativeToggleScreenplayEditor(true));
on("creativeCancelScreenplayBtn", "click", () => creativeToggleScreenplayEditor(false));
on("creativeSaveScreenplayBtn", "click", guard(creativeSaveScreenplay));
on("creativeApproveScreenplayBtn", "click", guard(creativeApproveScreenplay));
on("creativeSyncVisualsBtn", "click", guard(creativeSyncVisuals));
on("creativeRetryVisualsBtn", "click", guard(creativeRetryVisuals));
on("creativeProposeBibleBtn", "click", guard(creativeProposeBible));
on("creativeLockBibleBtn", "click", guard(creativeLockBible));
on("creativeProposeBeatsBtn", "click", guard(creativeProposeBeats));
on("creativeApproveBeatsBtn", "click", guard(creativeApproveBeats));
on("creativeGoDirectorBtn", "click", guard(creativeOpenInDirector));
on("creativeBeatList", "input", (event) => {
  if (event.target.matches("[data-shot-field]")) creativeRecordBeatEdit(event.target);
});
on("creativeBeatList", "change", (event) => {
  if (event.target.matches("[data-shot-field]")) creativeRecordBeatEdit(event.target);
});
on("creativeBriefEditor", "click", (event) => {
  const remove = event.target.closest("[data-cast-remove]");
  if (remove) { remove.closest("[data-cast-row]")?.remove(); return; }
  if (event.target.closest("[data-cast-add]")) {
    const cast = $("creativeBriefEditor").querySelector(".creative-cast");
    const index = cast.querySelectorAll("[data-cast-row]").length;
    if (index >= creativeMaxCast()) {
      setNotice("creativeBriefNotice", `<b>Cast is full</b>At most ${creativeMaxCast()} characters; each one needs its own key visual and identity lock.`, "is-warning");
      return;
    }
    const row = document.createElement("div");
    row.className = "creative-cast-row";
    row.dataset.castRow = String(index);
    row.innerHTML = `<input data-cast-field="name" placeholder="Name" /><input data-cast-field="role" placeholder="Role" /><input data-cast-field="look" placeholder="Look" /><button class="btn btn-tertiary" type="button" data-cast-remove="${index}" title="Remove">&times;</button>`;
    cast.insertBefore(row, cast.lastElementChild);
  }
});
window.addEventListener("pagehide", stopCreativePolling);
on("shotDeleteBtn", "click", guard(deleteSelectedShot));
document.addEventListener("click", (event) => {
  const deleteId = event.target.closest("[data-creative-delete]")?.dataset.creativeDelete;
  if (deleteId) { guard(deleteCreativeSession)(deleteId); return; }
  const sessionId = event.target.closest("[data-creative-session]")?.dataset.creativeSession;
  if (sessionId) guard(openCreativeSession)(sessionId);
  const skipAnchor = event.target.closest("[data-anchor-skip]")?.dataset.anchorSkip;
  if (skipAnchor) { guard(creativeSkipAnchor)(skipAnchor); return; }
  const regenerateAnchor = event.target.closest("[data-anchor-regenerate]")?.dataset.anchorRegenerate;
  if (regenerateAnchor) { guard(creativeRegenerateAnchor)(regenerateAnchor); return; }
  const replaceAnchor = event.target.closest("[data-anchor-replace]")?.dataset.anchorReplace;
  if (replaceAnchor) { creativeReplaceAnchor(replaceAnchor); return; }
  if (event.target.closest("[data-anchor-retry]")) { guard(creativeRetryVisuals)(); return; }
  const acceptCode = event.target.closest("[data-question-accept]")?.dataset.questionAccept;
  if (acceptCode) { guard(creativeResolveQuestion)(acceptCode, "ACCEPT_ASSUMPTION"); return; }
  const skipCode = event.target.closest("[data-question-skip]")?.dataset.questionSkip;
  if (skipCode) { guard(creativeResolveQuestion)(skipCode, "SKIP"); return; }
  const episodeId = event.target.closest("[data-episode-chip]")?.dataset.episodeChip;
  if (episodeId) guard(loadEpisode)(episodeId);
});

/* Episodes strip */
on("createNextEpisodeBtn", "click", openContinuationDialog);
on("cancelContinuationBtn", "click", () => $("continuationDialog").close());
on("prepareContinuationBtn", "click", guard(prepareContinuation));
on("confirmContinuationBtn", "click", guard(confirmContinuation));
document.querySelectorAll("[data-continuation-mode]").forEach((button) => {
  button.addEventListener("click", () => setContinuationMode(button.dataset.continuationMode));
});

/* Director */
on("compileBtn", "click", guard(compileScript));
on("generateBtn", "click", guard(generateShot));
on("refreshCandidatesBtn", "click", guard(async () => { await loadCandidates(); await renderShotStage(); }));
on("createCharacterBtn", "click", guard(createCharacter));
on("confirmCharacterBtn", "click", guard(confirmCharacterIdentity));
on("continuityBtn", "click", guard(continuity));
on("estimatedCost", "input", () => {
  $("barShotCost").textContent = `$${Number($("estimatedCost").value || 0).toFixed(2)}`;
});
on("viewScriptBtn", "click", () => {
  if (!$("scriptDrawer").open) $("scriptDrawer").showModal();
});
on("closeScriptDrawerBtn", "click", () => $("scriptDrawer").close());

/* Productions */
on("productionsRefreshBtn", "click", guard(refreshProductions));
on("closeMediaViewerBtn", "click", () => {
  $("mediaViewerDialog").close();
});
on("mediaViewerDialog", "close", () => {
  $("mediaViewerBody").innerHTML = "";
  if (mediaViewerObjectUrl) { URL.revokeObjectURL(mediaViewerObjectUrl); mediaViewerObjectUrl = null; }
});
on("loadJobBtn", "click", guard(loadGenerationJob));
on("retryJobBtn", "click", guard(() => mutateGenerationJob("retry")));
on("cancelJobBtn", "click", guard(() => mutateGenerationJob("cancel")));
on("reconcileJobBtn", "click", guard(() => mutateGenerationJob("reconcile")));
on("deleteJobBtn", "click", () => openDeleteCreationDialog(selectedJobId()));
on("cancelDeleteCreationBtn", "click", closeDeleteCreationDialog);
on("confirmDeleteCreationBtn", "click", guard(confirmDeleteCreation));
// Escape and the backdrop both close the dialog without deleting; clearing the
// pending id here means no later confirmation can act on a stale creation.
on("deleteCreationDialog", "close", () => { pendingDeleteJobId = null; });
// A row menu is dismissed by anything else the user does.
document.addEventListener("click", (event) => {
  if (!event.target.closest(".job-menu, .job-menu-panel")) closeJobMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeJobMenu();
});

/* Admin */
on("operationsRefreshBtn", "click", guard(loadOperations));
on("operationsShotSelect", "change", (event) => {
  if (event.target.value) guard(async () => { await selectShot(event.target.value); await loadShotAudit(); })();
});
on("loadShotAuditBtn", "click", guard(loadShotAudit));
on("operationsCharacterSelect", "change", (event) => {
  state.selectedCharacterId = event.target.value || null;
  renderCharacters();
});
on("loadNarrativeBtn", "click", guard(loadNarrativeState));
on("initializeNarrativeBtn", "click", guard(initializeNarrativeState));
on("directUploadBtn", "click", guard(directUploadAsset));

/* New-project dialog plumbing */
on("newProjectForm", "submit", submitNewProject);
on("cancelNewProjectBtn", "click", closeNewProjectDialog);
on("newProjectName", "input", () => {
  $("newProjectError").textContent = "";
  $("newProjectStatus").textContent = "";
  $("newProjectName").setAttribute("aria-invalid", "false");
});
on("newProjectDialog", "cancel", (event) => { if (projectCreationPending) event.preventDefault(); });
on("newProjectDialog", "close", () => {
  const returnFocus = newProjectReturnFocus;
  newProjectReturnFocus = null;
  if (returnFocus?.isConnected) requestAnimationFrame(() => returnFocus.focus());
});
on("newProjectDialog", "click", (event) => {
  if (event.target === $("newProjectDialog")) closeNewProjectDialog();
});

window.addEventListener("ai-director:plan-changed", (event) => {
  const workspace = state.authUser?.workspaces?.find((item) => item.id === event.detail?.workspaceId);
  if (!workspace || !event.detail?.planTier) return;
  workspace.plan_tier = event.detail.planTier;
  // Plan locks live server-side now: refetch both catalogues, and fall back
  // to a local re-render if the network refuses.
  Promise.all([loadPassengerModels(), loadImageTiers()]).catch(() => {
    renderPassengerModels();
    renderImageTierOptions();
  });
  loadCredits().catch(() => null);
});

/* The public shell owns /login and /signup; it tells us which one is showing. */
window.addEventListener("bestshiny:auth-route", (event) => {
  setAuthMode(event.detail.route === "/signup" ? "register" : "login");
});

onRoute((route) => {
  if (route !== "/app") return;
  if (state.authUser && !state.projects.length) startWorkspace().catch((error) => toast(error.message));
});

/* Boot */
switchPage("create");
setPassengerMedia("image");
syncJobFilterTabs();
setAuthMode(currentRoute() === "/signup" ? "register" : "login");
renderProductions();
bootstrapAuth().catch((error) => toast(error.message));
