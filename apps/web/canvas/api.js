/**
 * The canvas's HTTP client.
 *
 * Same conventions as app.js `request()`: the base URL rule, the session
 * cookie, the double-submit CSRF header on unsafe methods, and FastAPI's two
 * error shapes (`{detail: "..."}` and `{detail: {message, reason_code}}`).
 * A 401 is reported once through `onUnauthorized`; app.js stays the only
 * owner of the signed-in state.
 */

export const API_BASE = window.AI_DIRECTOR_API
  || (location.hostname === "127.0.0.1" && location.port === "18081"
    ? "http://127.0.0.1:18080"
    : "/api");

const CSRF_COOKIE_NAME = "ai_director_csrf";
const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/* The reference upload allowlist - the same table app.js holds
   (REFERENCE_IMAGE_TYPES), which tests pin to the server's `_IMAGE_TYPES`. */
export const IMAGE_TYPES = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
};
export const IMAGE_MAX_BYTES = 20 * 1024 * 1024;

function cookieValue(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie.split("; ").find((entry) => entry.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

export function csrfHeaders(method = "GET", headers = {}) {
  const result = { ...headers };
  if (UNSAFE.has(String(method).toUpperCase())) {
    const token = cookieValue(CSRF_COOKIE_NAME);
    if (token) result["X-CSRF-Token"] = token;
  }
  return result;
}

export class ApiError extends Error {
  constructor(message, { status = 0, detail = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.reasonCode = detail && typeof detail === "object" && !Array.isArray(detail) ? detail.reason_code || null : null;
  }
}

const FALLBACK_MESSAGES = {
  400: "The request was not accepted.",
  401: "Your session has expired. Sign in again.",
  402: "You are out of credits. Top up to keep generating.",
  403: "You don't have permission to do that in this workspace.",
  404: "That no longer exists.",
  409: "Something changed in the meantime. Try again.",
  413: "The file is larger than the server accepts.",
  422: "Some values were not accepted.",
  429: "Too many requests. Wait a moment and try again.",
  502: "The provider refused or could not be reached.",
};

/* Some older routes answer in Chinese; the product speaks English. */
const hasCJK = (text) => /[　-鿿＀-￯]/.test(text);

export function errorMessage(detail, status) {
  let message = "";
  if (Array.isArray(detail)) {
    // FastAPI request validation: [{loc, msg, type}].
    message = detail.map((item) => {
      const field = Array.isArray(item?.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
      return field ? `${field}: ${item?.msg || "invalid"}` : item?.msg || "";
    }).filter(Boolean).join("; ");
  } else if (detail && typeof detail === "object") {
    message = detail.message || "";
  } else if (typeof detail === "string") {
    message = detail;
  }
  if (!message || hasCJK(message)) {
    message = FALLBACK_MESSAGES[status] || (status ? `Request failed (${status}).` : "Could not reach BestShiny.");
  }
  return message;
}

let unauthorizedHandler = null;

/** Called with the ApiError of any 401 outside /api/auth. */
export function onUnauthorized(handler) {
  unauthorizedHandler = handler;
}

async function failure(response, path) {
  const payload = await response.json().catch(() => null);
  const detail = payload && typeof payload === "object" && "detail" in payload ? payload.detail : null;
  const error = new ApiError(errorMessage(detail, response.status), { status: response.status, detail });
  if (response.status === 401 && !path.startsWith("/api/auth/")) unauthorizedHandler?.(error);
  return error;
}

export async function request(path, { method = "GET", body, signal, keepalive = false } = {}) {
  const headers = csrfHeaders(method, body !== undefined ? { "Content-Type": "application/json" } : {});
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      credentials: "include",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      keepalive,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("Could not reach BestShiny. Check your connection and try again.", { status: 0 });
  }
  if (!response.ok) throw await failure(response, path);
  if (response.status === 204) return null;
  return response.json().catch(() => null);
}

const get = (path, options) => request(path, { ...options, method: "GET" });
const post = (path, body, options) => request(path, { ...options, method: "POST", body: body ?? {} });
const put = (path, body, options) => request(path, { ...options, method: "PUT", body });
const patch = (path, body, options) => request(path, { ...options, method: "PATCH", body });
const del = (path, options) => request(path, { ...options, method: "DELETE" });
const enc = encodeURIComponent;

/* ------------------------------------------------------------------ uploads */

export function imageFileProblem(file) {
  if (!file) return "Nothing landed - try dragging the file again.";
  const extension = (String(file.name || "").match(/\.[^.]+$/) || [""])[0].toLowerCase();
  const expected = IMAGE_TYPES[extension];
  if (!expected) return "That's not a supported image. Use a PNG, JPG or WebP file.";
  const declared = String(file.type || "").split(";")[0].trim().toLowerCase();
  if (declared !== expected) {
    return `That file is named ${extension} but its type is ${declared || "unknown"}. Use a PNG, JPG or WebP file.`;
  }
  if (file.size > IMAGE_MAX_BYTES) return "That image is over 20 MB. Use a smaller file.";
  return null;
}

/** A pasted screenshot has no usable name; give it one its type agrees with. */
export function nameClipboardImage(file) {
  if (!file || IMAGE_TYPES[(String(file.name || "").match(/\.[^.]+$/) || [""])[0].toLowerCase()]) return file;
  const extension = Object.entries(IMAGE_TYPES).find(([, type]) => type === file.type)?.[0];
  if (!extension) return file;
  return new File([file], `pasted-image${extension}`, { type: file.type, lastModified: Date.now() });
}

/** Upload a reference image into a project; resolves with the media asset. */
export async function uploadImage(projectId, file, { signal } = {}) {
  const form = new FormData();
  form.append("project_id", projectId);
  form.append("asset_type", "REFERENCE");
  form.append("file", file);
  let response;
  try {
    response = await fetch(`${API_BASE}/v1/assets`, {
      method: "POST", body: form, credentials: "include", headers: csrfHeaders("POST"), signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("The upload could not reach BestShiny. Check your connection and try again.", { status: 0 });
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = payload?.detail ?? null;
    // A 413 with no JSON came from a proxy in front of the api.
    const reason = response.status === 413 && !detail
      ? "the file is larger than the server accepts"
      : errorMessage(detail, response.status);
    const error = new ApiError(`Upload failed: ${reason}`, { status: response.status, detail });
    if (response.status === 401) unauthorizedHandler?.(error);
    throw error;
  }
  return response.json();
}

/** Fetch an authenticated media route as an object URL. */
export async function fetchBlobUrl(path, { signal } = {}) {
  const response = await fetch(`${API_BASE}${path}`, { credentials: "include", signal });
  if (!response.ok) {
    const error = new ApiError(errorMessage(null, response.status), { status: response.status });
    if (response.status === 401) unauthorizedHandler?.(error);
    throw error;
  }
  const blob = await response.blob();
  return { url: URL.createObjectURL(blob), mime: blob.type || "" };
}

/* ------------------------------------------------------------------ endpoints */

export const api = {
  me: () => get("/api/auth/me"),

  listProjects: () => get("/v1/projects"),
  createProject: (title) => post("/v1/projects", { title }),
  getProject: (projectId) => get(`/v1/projects/${enc(projectId)}`),
  billing: (workspaceId) => get(`/v1/workspaces/${enc(workspaceId)}/billing`),
  videoModels: (projectId) => get(`/v1/models?modality=video${projectId ? `&project_id=${enc(projectId)}` : ""}`),
  asset: (assetId) => get(`/v1/assets/${enc(assetId)}`),
  generation: (jobId) => get(`/v1/generations/${enc(jobId)}`),

  protocols: () => get("/v1/connections/protocols"),
  connections: (workspaceId) => get(`/v1/workspaces/${enc(workspaceId)}/connections`),
  createConnection: (workspaceId, body) => post(`/v1/workspaces/${enc(workspaceId)}/connections`, body),
  updateConnection: (workspaceId, connectionId, body) => patch(`/v1/workspaces/${enc(workspaceId)}/connections/${enc(connectionId)}`, body),
  deleteConnection: (workspaceId, connectionId) => del(`/v1/workspaces/${enc(workspaceId)}/connections/${enc(connectionId)}`),
  testConnection: (workspaceId, connectionId) => post(`/v1/workspaces/${enc(workspaceId)}/connections/${enc(connectionId)}/test`),
  remoteModels: (workspaceId, connectionId) => get(`/v1/workspaces/${enc(workspaceId)}/connections/${enc(connectionId)}/remote-models`),

  nodeTypes: () => get("/v1/workflows/node-types"),
  workflows: (projectId) => get(`/v1/projects/${enc(projectId)}/workflows`),
  createWorkflow: (projectId, body) => post(`/v1/projects/${enc(projectId)}/workflows`, body),
  workflow: (workflowId) => get(`/v1/workflows/${enc(workflowId)}`),
  saveWorkflow: (workflowId, body, options) => put(`/v1/workflows/${enc(workflowId)}`, body, options),
  deleteWorkflow: (workflowId) => del(`/v1/workflows/${enc(workflowId)}`),
  startRun: (workflowId, body) => post(`/v1/workflows/${enc(workflowId)}/runs`, body),
  runs: (workflowId, limit = 20) => get(`/v1/workflows/${enc(workflowId)}/runs?limit=${limit}`),
  run: (runId, options) => get(`/v1/workflow-runs/${enc(runId)}`, options),
  cancelRun: (runId) => post(`/v1/workflow-runs/${enc(runId)}/cancel`),
};
