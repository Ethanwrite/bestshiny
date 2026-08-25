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
  selectedCharacterId: null, page: "create", passengerMedia: "image", passengerOriginal: null,
  passengerPrompts: { image: "", video: "" }, passengerJobs: { image: null, video: null },
  passengerReferenceUpload: null, modelProfiles: [], imageModelProfiles: [], passengerModels: [],
  confirmedAssets: new Set(), logicalAssets: [],
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
  COMPILED: "Compiled", ACTION: "Action", DIALOGUE: "Dialogue", MEDIUM: "Medium",
  CLOSE_UP: "Close-up", WIDE: "Wide", EXTREME_CLOSE_UP: "Extreme close-up",
  COMMERCIAL_HERO: "Commercial hero",
  QUEUED: "Queued", SUBMITTED: "Submitted", RUNNING: "Generating", RETRY_WAIT: "Waiting to retry",
  COMPLETED: "Completed", CANCELLED: "Cancelled", WORKER_NEEDS_USER_ACTION: "Needs your action",
  GENERATING: "Generating",
  VALIDATING: "Checking", PASSED: "Passed", SOFT_FAILED: "Needs a fix", HARD_FAILED: "Rejected",
  PASS: "Passed", SOFT_FAIL: "Needs a fix", HARD_FAIL: "Rejected",
  USER_REVIEW_REQUIRED: "Needs your review", COMMITTED: "Approved", REJECTED: "Not used",
  FAILED: "Failed",
  NONE: "Standalone", PREVIOUS_END_FRAME: "From previous end frame",
  REFERENCE_FRAME: "From reference frame",
  HARD_CONTINUITY: "Hard continuity", HYBRID: "End frame + reference",
  RE_ANCHOR: "Re-anchor character and scene",
  CAMERA_AXIS_CHANGE: "Crosses the axis", SCENE_CHANGE: "Scene changes",
  TIMELINE_JUMP: "Time jump",
  LOW_PREVIOUS_FRAME_QUALITY: "Previous end frame is soft",
  LOW_PREVIOUS_FACE_VISIBILITY: "Face unclear in previous shot",
  IDENTITY_DRIFT_RISK: "Identity may drift", ACTION_DISCONTINUITY: "Action does not join",
  HIGH_CONTINUITY_RISK: "High continuity risk", SAME_SCENE: "Same scene",
  ACTION_CHAIN_CONTINUES: "Action continues",
  USABLE_END_FRAME: "Previous end frame is usable",
  MODERATE_CAMERA_OR_BLOCKING_CHANGE: "Camera or blocking moved",
  TEXT_TO_VIDEO: "Text to video", IMAGE_TO_VIDEO: "Image to video",
  CONTINUE_I2V: "Continue from previous frame", CONTINUE_V2V: "Continue from previous clip",
  HYBRID_REFERENCE: "End frame with reference",
  REANCHOR_CHARACTER: "Re-anchor character", REANCHOR_SCENE: "Re-anchor scene",
  REANCHOR_FULL: "Re-anchor character and scene",
  START_END_FRAME: "Start and end frame", REFERENCE_TO_VIDEO: "Reference to video",
  portrait: "Portrait", beauty_fashion: "Beauty & fashion", product: "Product",
  commercial: "Commercial", scene_concept: "Scene concept",
  reference_character_regeneration: "Identity preserving",
  CHARACTER: "Character", SCENE: "Scene", PRODUCT: "Product", PROP: "Prop", WARDROBE: "Wardrobe",
  VEHICLE: "Vehicle", CREATURE: "Creature", VOICE: "Voice", STYLE: "Style", REFERENCE: "Reference",
  google_flow: "Google Flow", seedance: "Seedance", veo_official: "Veo", grok: "Grok",
  kling: "Kling", runway: "Runway", omni: "Omni", wan: "Wan",
}[value] || value || "—");

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
    throw new Error(detail.detail || `Request failed (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}

function toast(message) {
  if (!message) return;
  $("toast").textContent = message;
  $("toast").classList.add("show");
  setTimeout(() => $("toast").classList.remove("show"), 2800);
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
  sessionStorage.removeItem(SUBMISSION_STORAGE_KEY);
  state.confirmedAssets.clear();
  $("projectSelect").innerHTML = '<option value="">No projects yet</option>';
  $("characterList").innerHTML = '<p class="empty-inline">No characters yet</p>';
  $("passengerExistingAsset").innerHTML = '<option value="">Create a new asset</option>';
  $("manualExistingAsset").innerHTML = '<option value="">Create a new asset</option>';
  $("manualAssetFile").value = "";
  $("manualAssetStatus").textContent = "A character's master reference can also be updated from the Director inspector.";
  $("lockProjectStyleBtn").disabled = true;
  $("projectStyleLockStatus").textContent = "Promote a style version to canonical first, then a project member can lock it explicitly.";
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
  workspaceLoad = Promise.all([loadProjects(), loadPassengerModels()])
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
    pill.innerHTML = "<i></i>API offline";
  }
}

async function loadCredits() {
  const workspace = (state.authUser?.workspaces || [])[0];
  if (!workspace) return;
  const billing = await request(`/v1/workspaces/${workspace.id}/billing`).catch(() => null);
  if (!billing) return;
  state.credits = billing.credit_balance;
  $("creditsAmount").textContent = `${Number(billing.credit_balance).toLocaleString()} CR`;
}

/* ============================================================
   Page switching
   ============================================================ */
const PAGE_HINT = {
  create: "Describe the frame, pick a model, generate.",
  director: "Compile a script, then direct one shot at a time.",
  productions: "Every generation job, with progress, cost and recovery.",
  admin: "Provider gateway, skills, evidence and verified uploads.",
};

function switchPage(page) {
  state.page = page;
  document.querySelectorAll("[data-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === page);
  });
  document.querySelectorAll("[data-page]").forEach((node) => {
    node.hidden = node.dataset.page !== page;
  });
  $("appBody").classList.toggle("no-inspector", page === "admin");
  $("appActionBar").hidden = page === "admin";
  if ($("modeDescription")) $("modeDescription").textContent = PAGE_HINT[page] || "";

  if (page === "admin" && state.authUser) loadOperations().catch((error) => toast(error.message));
  if (page === "productions") refreshProductions().catch((error) => toast(error.message));
}

/* ============================================================
   Create page
   ============================================================ */
function setPassengerMedia(media) {
  state.passengerPrompts[state.passengerMedia] = $("passengerPrompt").value;
  state.passengerMedia = media;
  $("passengerPrompt").value = state.passengerPrompts[media];
  document.querySelectorAll("[data-media]").forEach((button) => {
    button.classList.toggle("active", button.dataset.media === media);
  });
  const video = media === "video";
  $("passengerDurationField").classList.toggle("hidden", !video);
  $("barDurationFact").hidden = !video;
  $("imagePromptActions").classList.toggle("hidden", video);
  $("passengerGenerateBtn").textContent = video ? "Generate video" : "Generate image";
  $("passengerPromptHeading").textContent = video ? "Describe the motion" : "Describe the frame";
  $("passengerReferenceLabel").textContent = video ? "First frame or character reference" : "Reference image";
  $("passengerPrompt").placeholder = video
    ? "e.g. Slow push toward the bottle, backlight sweeps the glass edge, the logo stays stable"
    : "e.g. Rainy convenience-store doorway at night, a girl turns with a lit phone, 35mm film, cold and warm light meeting";
  $("promptTypeBadge").textContent = video ? "Sent as written" : "Auto intent";
  $("promptCorrectionSummary").textContent = video
    ? "Video prompts are never rewritten by the image rules. What you wrote is what is submitted."
    : "Only composition, light, material and depth are enhanced. Your subject is never redesigned.";
  renderPassengerModels();
  renderPassengerJob(state.passengerJobs[media]);
  updatePassengerCost();
}

async function loadPassengerModels() {
  const providers = await request("/v1/providers");
  const configuredProviders = providers.filter((provider) => provider.configured !== false);
  state.modelProfiles = configuredProviders.flatMap((provider) => (provider.models || [])
    .filter((model) => model.status !== "disabled"
      && model.modality === "video"
      && (model.supported_operations || []).includes("video_generation"))
    .map((model) => ({ ...model, provider: provider.name, media: "video" })));
  state.imageModelProfiles = configuredProviders.flatMap((provider) => (provider.models || [])
    .filter((model) => model.status !== "disabled"
      && model.modality === "image"
      && (model.supported_operations || []).includes("image_generation"))
    .map((model) => ({ ...model, provider: provider.name, media: "image" })));
  renderPassengerModels();
}

function renderPassengerModels() {
  state.passengerModels = state.passengerMedia === "image" ? state.imageModelProfiles : state.modelProfiles;
  const freeVideo = state.passengerMedia === "video"
    && state.authUser?.workspaces?.some((workspace) => workspace.plan_tier === "FREE");
  if (freeVideo) {
    state.passengerModels = state.passengerModels.filter((model) => model.provider === "seedance");
  }
  $("passengerModel").innerHTML = state.passengerModels.length
    ? state.passengerModels.map((model) => `<option value="${model.provider}|${model.model_id}">${simpleLabel(model.provider)} · ${escapeHTML(model.model_id)}</option>`).join("")
    : '<option value="">No models configured</option>';
  $("modelHint").textContent = freeVideo
    ? "Free plan video runs on Seedance. Upgrade to reach every route."
    : "The model you pick is never silently swapped.";
  updatePassengerCost();
}

function selectedPassengerModel() {
  const [provider, model] = $("passengerModel").value.split("|");
  return state.passengerModels.find((item) => item.provider === provider && item.model_id === model);
}

function passengerEstimatedCost() {
  const profile = selectedPassengerModel();
  if (!profile) return 0;
  const providerCost = state.passengerMedia === "image"
    ? Number(profile.cost?.estimated_per_image || .04)
    : Math.max(Number(profile.cost?.estimated_per_second || 0) * Number($("passengerDuration").value || 4), 0);
  const resolution = { "720p": 1, "1080p": 1.3 }[$("passengerResolution").value] || 1;
  const references = $("passengerReference").files[0] ? 1.04 : 1;
  return providerCost * resolution * references * 1.2;
}

function updatePassengerCost() {
  const profile = selectedPassengerModel();
  $("barAspect").textContent = $("passengerAspect").value;
  $("barResolution").textContent = $("passengerResolution").value;
  $("barDuration").textContent = `${$("passengerDuration").value || 4}s`;
  $("barModel").textContent = profile ? `${simpleLabel(profile.provider)} · ${profile.model_id}` : "None";
  $("advProvider").textContent = profile ? simpleLabel(profile.provider) : "—";
  $("advModelVersion").textContent = profile?.version || profile?.model_id || "—";
  $("advPricing").textContent = profile?.cost
    ? (state.passengerMedia === "image"
      ? `$${Number(profile.cost.estimated_per_image || 0).toFixed(3)} / image`
      : `$${Number(profile.cost.estimated_per_second || 0).toFixed(3)} / s`)
    : "provider settled";

  if (!profile) { $("passengerCost").textContent = "Pick a model"; return; }
  const estimate = passengerEstimatedCost();
  $("passengerCost").textContent = estimate > 0
    ? `${Math.max(1, Math.ceil(estimate / .01))} CR · $${estimate.toFixed(2)}`
    : "Settled on the provider account";
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
    $("promptTypeBadge").textContent = result.model_refinement?.accepted ? "Model refined" : "Rule refined";
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

/* An empty state that cannot be acted on is just a label. Both branches here
   end in a button that moves the user forward. */
function emptyCanvasMarkup() {
  if (!state.project) {
    return `
      <div class="empty-block">
        <span class="empty-icon" aria-hidden="true">◇</span>
        <strong>Create a project first</strong>
        <p>Frames, shots, characters and credits all belong to a project. Make one and the canvas opens up.</p>
        <div class="btn-row btn-row-center">
          <button class="btn btn-secondary" type="button" data-empty-action="new-project">Create a project</button>
        </div>
      </div>`;
  }
  return `
    <div class="empty-block">
      <span class="empty-icon" aria-hidden="true">▣</span>
      <strong>Nothing generated yet</strong>
      <p>Start from a written frame, or drop a reference image to work from.</p>
      <div class="btn-row btn-row-center">
        <button class="btn btn-secondary" type="button" data-empty-action="prompt">Start from prompt</button>
        <button class="btn btn-secondary" type="button" data-empty-action="upload">Upload an image</button>
      </div>
    </div>`;
}

async function renderPassengerJob(job) {
  const stage = $("passengerResult");
  if (!job) {
    if (state.passengerPreviewObjectUrl) URL.revokeObjectURL(state.passengerPreviewObjectUrl);
    state.passengerPreviewObjectUrl = null;
    stage.className = "canvas-stage empty-state";
    stage.innerHTML = emptyCanvasMarkup();
    $("saveToProjectBtn").disabled = true;
    $("promotePassengerAssetBtn").disabled = true;
    $("promotePassengerAssetBtn").textContent = "Save version";
    return;
  }
  rememberJob(job);
  $("operationsJobId").value = job.id;
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
      preview = `<img class="result-preview" src="${escapeHTML(mediaUrl)}" alt="Generated result" />`;
    } else if (mediaUrl && asset.mime_type?.startsWith("video/")) {
      preview = `<video class="result-preview" src="${escapeHTML(mediaUrl)}" controls playsinline></video>`;
    }
  }

  const reconciling = job.credit_status === "RECONCILIATION_REQUIRED";
  const displayedStatus = reconciling ? "Reconciling · credits held" : simpleLabel(job.status);
  const tone = reconciling ? "is-queued" : statusTone(job.status);
  stage.className = "canvas-stage has-result";
  stage.innerHTML = `
    ${preview || `<div class="empty-block"><span class="empty-icon" aria-hidden="true">◷</span><strong>${escapeHTML(displayedStatus)}</strong><p>The result appears here as soon as the provider returns it.</p></div>`}
    <div class="result-bar">
      <span class="status-chip ${tone}">${escapeHTML(displayedStatus)}</span>
      <div class="result-meta">
        <div><span>Model</span><strong>${simpleLabel(job.provider)} · ${escapeHTML(job.model || "—")}</strong></div>
        <div><span>Job</span><strong>${escapeHTML(job.id)}</strong></div>
        <div><span>Output asset</span><strong>${escapeHTML(job.output_asset_id || "pending")}</strong></div>
      </div>
    </div>`;
  const confirmed = state.confirmedAssets.has(job.output_asset_id);
  $("saveToProjectBtn").disabled = !job.output_asset_id || confirmed;
  $("saveToProjectBtn").textContent = confirmed ? "Saved to project" : "Save to project";
  $("promotePassengerAssetBtn").disabled = !job.output_asset_id || confirmed;
}

async function generatePassenger() {
  if (!state.project) return toast("Create a project first");
  const prompt = $("passengerPrompt").value.trim();
  const selection = selectedPassengerModel();
  if (!prompt || !selection) return toast("Write a prompt and pick a model");
  const projectId = state.project.id;
  const mediaType = state.passengerMedia;
  const aspectRatio = $("passengerAspect").value;
  const resolution = $("passengerResolution").value;
  const duration = mediaType === "video" ? Number($("passengerDuration").value || 4) : null;
  const negativePrompt = $("passengerNegativePrompt").value.trim();
  const criticality = $("passengerCriticality").value;
  const estimatedCost = passengerEstimatedCost();
  const freeVideo = mediaType === "video"
    && state.authUser?.workspaces?.some((workspace) => workspace.plan_tier === "FREE");
  const file = $("passengerReference").files[0];
  const fingerprint = JSON.stringify({
    projectId, mediaType, provider: selection.provider, model: selection.model_id,
    modelRole: freeVideo ? "VIDEO_SEEDANCE" : null,
    prompt, negativePrompt, criticality, aspectRatio, resolution, duration, estimatedCost,
    file: file ? [file.name, file.size, file.lastModified] : null,
  });
  const idempotencyKey = beginSubmission("passenger", fingerprint);
  if (!idempotencyKey) return;
  const button = $("passengerGenerateBtn");
  button.disabled = true;
  button.textContent = "Submitting…";
  ["passengerPrompt", "passengerModel", "passengerAspect", "passengerResolution",
    "passengerDuration", "passengerReference"].forEach((id) => { $(id).disabled = true; });
  let succeeded = false;
  try {
    const reference = await uploadPassengerReference({ projectId, file });
    const payload = {
      project_id: projectId,
      media_type: mediaType,
      provider: selection.provider,
      model: selection.model_id,
      ...(freeVideo ? { model_role: "VIDEO_SEEDANCE" } : {}),
      prompt,
      ...(negativePrompt ? { negative_prompt: negativePrompt } : {}),
      asset_criticality: criticality,
      aspect_ratio: aspectRatio,
      resolution,
      reference_asset_ids: reference ? [reference] : [],
      idempotency_key: idempotencyKey,
      estimated_cost: estimatedCost,
    };
    if (duration !== null) payload.duration = duration;
    const job = await request("/api/passenger/generate", { method: "POST", body: JSON.stringify(payload) });
    state.passengerJobs[mediaType] = job;
    await renderPassengerJob(job);
    await loadCredits();
    succeeded = true;
    toast(freeVideo
      ? "Submitted. Free plan video runs on Seedance."
      : "Submitted. Your model choice is not substituted.");
  } finally {
    finishSubmission("passenger", idempotencyKey, succeeded);
    button.disabled = false;
    ["passengerPrompt", "passengerModel", "passengerAspect", "passengerResolution",
      "passengerDuration", "passengerReference"].forEach((id) => { $(id).disabled = false; });
    button.textContent = state.passengerMedia === "video" ? "Generate video" : "Generate image";
  }
}

async function refreshPassengerJob() {
  const current = state.passengerJobs[state.passengerMedia];
  if (!current) return toast("No generation running");
  const job = await request(`/v1/generations/${current.id}`);
  state.passengerJobs[state.passengerMedia] = job;
  await renderPassengerJob(job);
}

async function confirmPassengerAsset() {
  const job = state.passengerJobs[state.passengerMedia];
  if (!job?.output_asset_id) return toast("Wait for the generation to finish");
  const name = $("passengerAssetName").value.trim() || `${state.passengerMedia === "image" ? "Image" : "Video"} asset`;
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
  await loadLogicalAssets();
  await renderPassengerJob(job);
  $("saveAssetDialog").close();
  toast(result.canonical ? "Version saved and set as canonical" : "Version saved to the project");
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
  $("passengerExistingAsset").innerHTML = options(" · canonical");
  $("manualExistingAsset").innerHTML = options(" · canonical");
  renderAssetRail();
  renderProjectStyleLock();
}

function renderAssetRail() {
  const rail = $("assetRail");
  if (!rail) return;
  if (!state.logicalAssets.length) {
    rail.classList.add("empty");
    rail.innerHTML = '<p class="empty-inline">No assets yet. Generate a frame and save it to the project.</p>';
    return;
  }
  rail.classList.remove("empty");
  rail.innerHTML = state.logicalAssets.map((asset) => `
    <button class="asset-chip" type="button" data-asset="${escapeHTML(asset.id)}">
      <span class="asset-kind" style="color:${assetKindColor(asset.asset_type)}">${escapeHTML(simpleLabel(asset.asset_type)).toUpperCase()}</span>
      <span class="asset-name">${escapeHTML(asset.name)}</span>
      ${asset.canonical_version_id ? '<span class="asset-flag">●</span>' : ""}
    </button>`).join("");
  rail.querySelectorAll("[data-asset]").forEach((button) => {
    button.addEventListener("click", () => openAssetDetails(button.dataset.asset));
  });
}

function assetKindColor(type) {
  return ({
    CHARACTER: "var(--violet)", SCENE: "var(--ok)", PRODUCT: "var(--info)",
    STYLE: "var(--brand)", WARDROBE: "#f2708b", PROP: "#e8c96b",
  })[type] || "var(--fg-meta)";
}

function renderProjectStyleLock() {
  const selected = state.logicalAssets.find((asset) => asset.id === $("manualExistingAsset").value);
  const lockable = selected?.asset_type === "STYLE" && selected.canonical_version_id;
  $("lockProjectStyleBtn").disabled = Boolean(state.styleLock?.locked) || !lockable;
  $("lockProjectStyleBtn").textContent = state.styleLock?.locked ? "Project style is locked" : "Lock as the project's style";
  $("projectStyleLockStatus").textContent = state.styleLock?.locked
    ? `Locked to version ${state.styleLock.style_version_id.slice(0, 8)}. Later shots inherit it and are checked for drift.`
    : (lockable
      ? "Locking is permanent. A style embedding is extracted and applied as a gate on every later generation."
      : "Promote a style version to canonical first, then a project member can lock it explicitly.");
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
    $("assetVersionList").textContent = "Select an asset to see its versions.";
    return;
  }
  $("manualAssetType").value = selected.asset_type;
  $("manualAssetName").value = selected.name;
  $("assetCurrentName").textContent = selected.name;
  $("assetCurrentMeta").textContent = `${simpleLabel(selected.asset_type).toUpperCase()} · ${selected.canonical_version_id ? "canonical set" : "no canonical yet"}`;

  const detail = await request(`/api/assets/${selected.id}`).catch(() => null);
  const versions = detail?.versions || [];
  const list = $("assetVersionList");
  if (!versions.length) {
    list.className = "version-list empty-state";
    list.textContent = "No versions saved yet.";
    return;
  }
  list.className = "version-list";
  list.innerHTML = versions.slice().reverse().map((version) => {
    const canonical = version.id === selected.canonical_version_id;
    return `<div class="version-row">
      <span class="version-no mono">v${escapeHTML(String(version.version))}</span>
      <span class="version-label">${escapeHTML(version.label || simpleLabel(version.source))}</span>
      ${canonical
        ? '<span class="status-chip is-ok">Canonical</span>'
        : `<button class="btn btn-tertiary" type="button" data-promote-version="${escapeHTML(version.id)}">Set as canonical</button>`}
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
  toast("Canonical reference updated. Earlier versions are kept.");
}

async function lockSelectedProjectStyle() {
  if (!state.project) return toast("Create a project first");
  const selected = state.logicalAssets.find((asset) => asset.id === $("manualExistingAsset").value);
  if (selected?.asset_type !== "STYLE" || !selected.canonical_version_id) {
    return toast("Pick a style asset that already has a canonical version");
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
        label: `User upload v${Date.now()}`,
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
    $("manualAssetStatus").textContent = `Saved ${simpleLabel(assetType)} "${assetName}" as v${version.version}${promoted ? " and set it as canonical." : ". The canonical reference did not change."}`;
    toast("New version saved. Earlier versions stay traceable.");
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
    $("passengerReference").value = "";
    $("referenceFileName").hidden = true;
    renderPassengerJob(null);
  }
  state.project = await request(`/v1/projects/${id}`);
  $("projectSelect").value = id;
  await loadLogicalAssets();
  await loadCharacters();
  if (state.project.episodes.length) await loadEpisode(state.project.episodes[0].id);
  else resetProductionView();
  if (!state.passengerJobs[state.passengerMedia]) await renderPassengerJob(null);
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

function resetProductionView() {
  state.episode = null; state.shot = null; state.candidates = [];
  $("scriptPanel").hidden = false;
  $("shotTreePanel").hidden = true;
  $("viewScriptBtn").hidden = true;
  $("compileBtn").disabled = !state.project;
  $("scriptInput").disabled = !state.project;
  $("sceneList").className = "shot-tree empty-state";
  $("sceneList").textContent = "Compile a script to see scenes and shots.";
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
    tree.textContent = "Compile a script to see scenes and shots.";
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
  $("shotStageMedia").innerHTML = `
    <div class="empty-block">
      <span class="empty-icon" aria-hidden="true">◻</span>
      <strong>No shot selected</strong>
      <p>${!state.project
        ? "Scenes and shots belong to a project. Create one to start directing."
        : hasShots
          ? "Pick a shot from the tree on the left to direct it."
          : "Paste a script on the left and compile it into an ordered shot list."}</p>
      <div class="btn-row btn-row-center">
        ${!state.project
          ? '<button class="btn btn-secondary" type="button" data-empty-action="new-project">Create a project</button>'
          : hasShots
            ? '<button class="btn btn-secondary" type="button" data-empty-action="first-shot">Choose a shot</button>'
            : '<button class="btn btn-secondary" type="button" data-empty-action="compile">Generate shots</button>'}
      </div>
    </div>`;
  $("shotNumber").textContent = "SHOT —";
  $("shotAction").textContent = "Select a shot";
  $("shotState").textContent = "Opening state → one action → closing state";
  $("shotTitle").textContent = "No shot selected";
  $("shotPrompt").textContent = "Compile a script on the left and the system builds an ordered, producible shot list, remembering the state each shot starts and ends in.";
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
  $("shotProvider").textContent = simpleLabel(shot.provider);
  $("compShotType").textContent = simpleLabel(shot.shot_type);
  $("compInputState").textContent = shot.input_state ? "Set" : "Not set";
  $("compOutputState").textContent = shot.output_state ? "Planned" : "Not set";
  $("shotModelProvider").textContent = simpleLabel(shot.provider);
  $("shotModelPolicy").textContent = simpleLabel(shot.generation_policy);
  $("shotModelContinuity").textContent = simpleLabel(shot.continuity_policy);
  $("rawPrompt").value = shot.user_prompt;
  $("compiledPrompt").value = shot.compiled_prompt || "";
  $("barShotDuration").textContent = `${shot.duration}s`;
  $("barShotModel").textContent = simpleLabel(shot.provider);
  $("generateBtn").disabled = false;
  $("generateBtn").textContent = shot.status === "COMMITTED" ? "Regenerate shot" : "Generate shot";

  await loadCandidates();
  await renderShotStage();
  syncOperationsContext();
}

/** The current shot carries the page: show the approved take when there is one. */
async function renderShotStage() {
  const stage = $("shotStageMedia");
  if (shotStageObjectUrl) { URL.revokeObjectURL(shotStageObjectUrl); shotStageObjectUrl = null; }
  const committed = state.candidates.find((candidate) => candidate.status === "COMMITTED" && candidate.output_asset_id)
    || state.candidates.find((candidate) => candidate.output_asset_id);
  if (!committed) {
    const generating = state.candidates.some((candidate) => ["QUEUED", "RUNNING", "GENERATING", "VALIDATING"].includes(candidate.status));
    stage.innerHTML = `
      <div class="empty-block">
        <span class="empty-icon" aria-hidden="true">${generating ? "◷" : "◻"}</span>
        <strong>${generating ? "Generating this shot" : "Nothing generated for this shot"}</strong>
        <p>${generating
          ? "Variants appear below as the provider returns them."
          : "Generate the shot to see the take here, then approve one variant into the timeline."}</p>
      </div>`;
    return;
  }
  const media = await resolveAssetMedia(committed.output_asset_id);
  if (!media) {
    stage.innerHTML = '<div class="empty-block"><strong>Result is not previewable</strong><p>The output asset exists but no preview could be loaded.</p></div>';
    return;
  }
  if (media.revocable) shotStageObjectUrl = media.url;
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

function renderCandidates(candidates) {
  const grid = $("candidateGrid");
  if (!candidates.length) {
    grid.className = "variant-grid empty-state";
    grid.innerHTML = `
      <div class="empty-block">
        <strong>No variants yet</strong>
        <p>Generate this shot to compare A / B / C with quality checks and cost.</p>
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
      <div class="output-box">${escapeHTML(qa.summary || "Waiting for generation or checks")}<br>$${candidate.cost.toFixed(2)} · ${Math.max(1, Math.ceil(candidate.cost / .01))} CR</div>
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
  let episodeId = state.project.episodes[0]?.id;
  if (!episodeId) {
    const episode = await request(`/v1/projects/${state.project.id}/episodes`, {
      method: "POST",
      body: JSON.stringify({ project_id: state.project.id, title: "Episode 1", episode_number: 1, script_source: script }),
    });
    episodeId = episode.id;
  } else if (state.episode?.script_source !== script) {
    toast("Existing shots are protected: the script is not overwritten. Create a new project to compile a different script.");
    return;
  }
  await request(`/v1/episodes/${episodeId}/compile`, { method: "POST", body: "{}" });
  await selectProject(state.project.id);
  toast("Script compiled into scenes and shots, with the join between each shot recorded.");
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
  $("characterList").innerHTML = state.characters.length ? state.characters.map((character) => {
    const latest = character.identity_versions.at(-1);
    const selected = character.id === state.selectedCharacterId ? " selected" : "";
    const identity = latest
      ? `Identity v${latest.version} locked · upload to create v${latest.version + 1}`
      : "No master reference locked yet";
    return `<button class="binding${selected}" type="button" data-character="${character.id}"><strong>${escapeHTML(character.name)}</strong><span>${identity}</span></button>`;
  }).join("") : '<p class="empty-inline">No characters yet. Add one so later shots can hold the same face.</p>';
  $("characterList").querySelectorAll("[data-character]").forEach((button) => button.addEventListener("click", () => {
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
   Productions — user-facing job list.

   There is no server-side "list my generations" endpoint, and adding one
   would be a backend change. So Productions tracks the jobs this workspace
   started in this session plus every job referenced by a shot candidate, and
   refreshes each against /v1/generations/{id}. Real data, no invention.
   ============================================================ */
function rememberJob(job) {
  if (!job?.id) return;
  const previous = state.jobs.get(job.id) || {};
  state.jobs.set(job.id, { ...previous, ...job });
}

const JOB_BUCKET = {
  RUNNING: "running", GENERATING: "running", SUBMITTED: "running", VALIDATING: "running",
  QUEUED: "queued", NEW: "queued", RESERVED: "queued", RETRY_WAIT: "queued",
  COMPLETED: "completed", COMMITTED: "completed", PASSED: "completed",
  FAILED: "failed", HARD_FAILED: "failed", CANCELLED: "failed",
};
const bucketOf = (job) => JOB_BUCKET[job.status] || "queued";

function jobProgress(job) {
  return ({ queued: 8, running: 55, completed: 100, failed: 100 })[bucketOf(job)] || 0;
}

async function refreshProductions() {
  const ids = [...state.jobs.keys()];
  const fresh = await Promise.all(ids.map((id) => request(`/v1/generations/${encodeURIComponent(id)}`).catch(() => null)));
  fresh.forEach((job, index) => {
    if (job) rememberJob({ ...state.jobs.get(ids[index]), ...job });
  });
  renderProductions();
}

function renderProductions() {
  const list = $("productionsList");
  if (!list) return;
  const jobs = [...state.jobs.values()];
  const counts = { running: 0, queued: 0, completed: 0, failed: 0 };
  jobs.forEach((job) => { counts[bucketOf(job)] += 1; });
  $("prodCountRunning").textContent = counts.running;
  $("prodCountQueued").textContent = counts.queued;
  $("prodCountCompleted").textContent = counts.completed;
  $("prodCountFailed").textContent = counts.failed;
  $("barJobCount").textContent = `${jobs.length} job${jobs.length === 1 ? "" : "s"}`;
  const spend = jobs.reduce((total, job) => total + Number(job.cost || 0), 0);
  $("barJobSpend").textContent = `${Math.ceil(spend / .01) || 0} CR`;

  const visible = state.jobFilter === "all" ? jobs : jobs.filter((job) => bucketOf(job) === state.jobFilter);
  if (!visible.length) {
    list.className = "job-list empty-state";
    list.innerHTML = `
      <div class="empty-block">
        <span class="empty-icon" aria-hidden="true">◷</span>
        <strong>${jobs.length ? "Nothing in this state" : "No production jobs yet"}</strong>
        <p>${jobs.length
          ? "Switch the filter to see the jobs you do have."
          : "Jobs appear here as soon as you generate a frame or a shot."}</p>
        <div class="btn-row btn-row-center">
          <button class="btn btn-secondary" type="button" data-empty-action="go-create">Go to Create</button>
          <button class="btn btn-secondary" type="button" data-empty-action="go-director">Go to Director</button>
        </div>
      </div>`;
    return;
  }
  list.className = "job-list";
  list.innerHTML = visible.map((job) => {
    const bucket = bucketOf(job);
    const tone = statusTone(job.status);
    const credits = job.cost ? `${Math.max(1, Math.ceil(job.cost / .01))} CR` : "—";
    return `<button class="job-card ${state.selectedJobId === job.id ? "active" : ""}" type="button" data-job="${escapeHTML(job.id)}">
      <span class="job-rail ${tone}"></span>
      <span class="job-main">
        <span class="job-title">
          <strong>${escapeHTML(job.shotLabel || job.model || "Generation")}</strong>
          <span class="status-chip ${tone}">${simpleLabel(job.status)}</span>
        </span>
        <span class="job-sub mono">${escapeHTML(job.id)}${job.provider ? ` · ${escapeHTML(simpleLabel(job.provider))}` : ""}</span>
      </span>
      <span class="job-side">
        ${bucket === "running" || bucket === "queued"
          ? `<span class="job-progress"><i style="width:${jobProgress(job)}%"></i></span>`
          : ""}
        <span class="job-cost mono">${credits}</span>
      </span>
    </button>`;
  }).join("");
  list.querySelectorAll("[data-job]").forEach((button) => {
    button.addEventListener("click", guard(() => selectJob(button.dataset.job)));
  });
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

function renderGenerationControl(job) {
  state.operations.job = job;
  if (!job) {
    setText("operationsJobMetric", "None");
    $("generationControlStatus").className = "output-box empty-state";
    $("generationControlStatus").textContent = "Select a job, or paste a job ID on the left.";
    ["retryJobBtn", "cancelJobBtn", "reconcileJobBtn"].forEach((id) => { $(id).disabled = true; });
    return;
  }
  rememberJob(job);
  setText("operationsJobMetric", simpleLabel(job.status));
  $("operationsJobId").value = job.id;
  $("generationControlStatus").className = "output-box";
  $("generationControlStatus").innerHTML = `
    <span class="status-chip ${statusTone(job.status)}">${simpleLabel(job.status)}</span><br>
    Provider ${escapeHTML(simpleLabel(job.provider))} · model ${escapeHTML(job.model || "—")}<br>
    Submission ${escapeHTML(simpleLabel(job.submission_state))} · credits ${escapeHTML(simpleLabel(job.credit_status))}<br>
    Attempts ${Number(job.attempt_count || 0)}
    ${job.error_message ? `<br><span style="color:var(--danger)">${escapeHTML(job.error_message)}</span>` : ""}`;
  $("retryJobBtn").disabled = job.safe_to_retry !== true;
  $("cancelJobBtn").disabled = !["QUEUED", "SUBMITTED", "RUNNING", "RETRY_WAIT"].includes(job.status);
  $("reconcileJobBtn").disabled = !["SENT_UNCONFIRMED", "SUBMITTED"].includes(job.submission_state) && !["FAILED", "RUNNING"].includes(job.status);

  const events = job.events || [];
  const list = $("generationEvents");
  list.className = events.length ? "event-list" : "event-list empty-state";
  list.innerHTML = events.length
    ? events.map((event) => `<div class="event-item"><strong>${escapeHTML(simpleLabel(event.type))}</strong><small>${escapeHTML(event.created_at || "")}</small><div>${jsonView(event.detail || {})}</div></div>`).join("")
    : "No events on this job";
  renderProductions();
}

async function loadGenerationJob() {
  const id = selectedJobId();
  if (!id) return toast("Paste a generation job ID");
  renderGenerationControl(await request(`/v1/generations/${encodeURIComponent(id)}`));
}

async function mutateGenerationJob(action) {
  const id = selectedJobId();
  if (!id) return toast("Load a job first");
  await request(`/v1/generations/${encodeURIComponent(id)}/${action}`, { method: "POST", body: "{}" });
  await loadGenerationJob();
  await loadCredits();
  toast(({
    retry: "Job re-entered safe retry",
    cancel: "Cancellation processed",
    reconcile: "Job state reconciled",
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
    $("passwordResetStatus").textContent += " Development token filled in automatically.";
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
    document.querySelectorAll("[data-job-filter]").forEach((item) => item.classList.toggle("active", item === button));
    renderProductions();
  });
});

/* Empty states always offer the next move; this is where those moves land. */
document.addEventListener("click", (event) => {
  const action = event.target.closest("[data-empty-action]")?.dataset.emptyAction;
  if (!action) return;
  if (action === "new-project") openNewProjectDialog();
  if (action === "prompt") { switchPage("create"); $("passengerPrompt").focus(); }
  if (action === "upload") { switchPage("create"); $("passengerReference").click(); }
  if (action === "go-create") switchPage("create");
  if (action === "go-director") switchPage("director");
  if (action === "compile") { switchPage("director"); $("scriptInput").focus(); }
  if (action === "first-shot") {
    const first = document.querySelector("[data-shot]");
    if (first) first.click();
  }
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
const dropzone = document.querySelector(".dropzone");
if (dropzone) {
  ["dragenter", "dragover"].forEach((type) => dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dropzone.classList.add("is-over");
  }));
  ["dragleave", "drop"].forEach((type) => dropzone.addEventListener(type, () => dropzone.classList.remove("is-over")));
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    const file = event.dataTransfer?.files?.[0];
    if (!file) return;
    const transfer = new DataTransfer();
    transfer.items.add(file);
    $("passengerReference").files = transfer.files;
    $("passengerReference").dispatchEvent(new Event("change"));
  });
}
on("saveToProjectBtn", "click", () => {
  const job = state.passengerJobs[state.passengerMedia];
  if (!job?.output_asset_id) return toast("Wait for the generation to finish");
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
on("loadJobBtn", "click", guard(loadGenerationJob));
on("retryJobBtn", "click", guard(() => mutateGenerationJob("retry")));
on("cancelJobBtn", "click", guard(() => mutateGenerationJob("cancel")));
on("reconcileJobBtn", "click", guard(() => mutateGenerationJob("reconcile")));

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
  renderPassengerModels();
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
setAuthMode(currentRoute() === "/signup" ? "register" : "login");
renderProductions();
bootstrapAuth().catch((error) => toast(error.message));
