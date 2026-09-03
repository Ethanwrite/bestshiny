import { currentUser, isAdminRoute, navigate, onRoute } from "./router.js";

// Same convention as app.js: the served origin proxies /api/ to the API and
// strips that prefix, so the base must carry it for `${API}/api${path}` to
// arrive as /api/admin/... rather than /admin/...
const API = window.AI_DIRECTOR_API
  || (location.hostname === "127.0.0.1" && location.port === "18081"
    ? "http://127.0.0.1:18080"
    : "/api");
const $ = (id) => document.getElementById(id);
const esc = (value = "") => String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
const fmt = (value) => {
  if (value == null) return "—";
  return typeof value === "number" && Number.isFinite(value)
    ? new Intl.NumberFormat().format(value)
    : String(value);
};
const date = (value) => value ? new Date(value).toLocaleString() : "—";
const status = (value) => `<span class="admin-status" data-status="${esc(value || "UNKNOWN")}">${esc(value || "UNKNOWN")}</span>`;

const PAGE = {
  overview: { title: "Overview", subtitle: "Live operational facts with explicit time windows and coverage.", endpoint: "/admin/dashboard", filters: false },
  users: { title: "Users", subtitle: "Accounts, plans, balances and recent activity across all workspaces.", endpoint: "/admin/users", filters: true, statuses: ["ACTIVE", "SUSPENDED"] },
  credits: { title: "Credits & Billing", subtitle: "Reservation, charge, release, reconciliation and operator-ledger events.", endpoint: "/admin/credits", filters: true, statuses: ["RESERVED", "SETTLED", "REFUNDED", "RECONCILIATION_REQUIRED"] },
  models: { title: "Model Registry", subtitle: "The persisted model truth: lifecycle, production eligibility, cost and capabilities.", endpoint: "/admin/models", filters: true, statuses: ["DISABLED", "CONFIGURED", "TESTING", "VERIFIED", "LIVE", "DEGRADED", "BLOCKED"] },
  providers: { title: "Providers", subtitle: "Credential presence, non-billable probes and runtime health. Secrets never enter this page.", endpoint: "/admin/providers", filters: false },
  routing: { title: "Routing", subtitle: "Automatic-selection eligibility and real evidence; scores remain request-dependent.", endpoint: "/admin/routing", filters: false },
  jobs: { title: "Generation Jobs", subtitle: "Cross-tenant execution, billing lifecycle, failure evidence and safe commands.", endpoint: "/admin/jobs", filters: true, statuses: ["NEW", "RESERVED", "QUEUED", "SUBMITTED", "RUNNING", "RETRY_WAIT", "COMPLETED", "FAILED", "CANCELLED"] },
  projects: { title: "Project Inspection", subtitle: "Read-only support and troubleshooting view of user productions.", endpoint: "/admin/projects", filters: true, statuses: ["ACTIVE", "ARCHIVED", "SUSPENDED"] },
  system: { title: "System Health", subtitle: "Real probes and explicit Unknown / Not Configured states—never a fabricated all-green panel.", endpoint: "/admin/system", filters: false },
  audit: { title: "Audit Logs", subtitle: "Append-only history of every high-impact platform mutation.", endpoint: "/admin/audit", filters: true, statuses: [] },
};

let activePage = "overview";
let activePath = "/admin";
let pendingConfirm = null;
let activeDetail = null;

function csrf() {
  const match = document.cookie.split("; ").find((item) => item.startsWith("ai_director_csrf="));
  return match ? decodeURIComponent(match.split("=").slice(1).join("=")) : "";
}

async function request(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (!["GET", "HEAD"].includes((options.method || "GET").toUpperCase())) headers["X-CSRF-Token"] = csrf();
  const response = await fetch(`${API}/api${path}`, { credentials: "include", ...options, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}

function parseRoute(route) {
  const parts = route.split("/").filter(Boolean);
  return { page: parts[1] || "overview", id: parts[2] || null };
}

function queryFor(page) {
  const params = new URLSearchParams();
  const search = $("adminSearch").value.trim();
  const state = $("adminStatusFilter").value;
  if (search && ["users", "models", "projects", "audit"].includes(page)) params.set("q", search);
  if (search && ["credits", "jobs"].includes(page)) {
    const separator = search.indexOf(":");
    const prefix = separator > 0 ? search.slice(0, separator).trim().toLowerCase() : "";
    const value = separator > 0 ? search.slice(separator + 1).trim() : search;
    const fields = page === "credits"
      ? { user: "user_id", workspace: "workspace_id", project: "project_id", generation: "generation_id" }
      : { user: "user_id", project: "project_id", provider: "provider" };
    params.set(fields[prefix] || (page === "credits" ? "generation_id" : "project_id"), value);
  }
  if (state) {
    if (page === "models") params.set("lifecycle", state);
    else if (["users", "jobs", "projects"].includes(page)) params.set("status", state);
    else if (page === "credits") params.set("event_type", state);
  }
  if (["credits", "jobs", "audit"].includes(page)) {
    const from = $("adminDateFrom").value;
    const to = $("adminDateTo").value;
    if (from) params.set("created_from", new Date(from).toISOString());
    if (to) params.set("created_to", new Date(to).toISOString());
  }
  return params.toString() ? `?${params}` : "";
}

async function load(route = activePath) {
  activePath = route;
  const parsed = parseRoute(route);
  activePage = PAGE[parsed.page] ? parsed.page : "overview";
  const config = PAGE[activePage];
  $("adminTitle").textContent = config.title;
  $("adminSubtitle").textContent = config.subtitle;
  $("adminFilterbar").hidden = !config.filters;
  $("adminSearch").value = "";
  $("adminSearch").placeholder = activePage === "credits" ? "user:<id>, project:<id>, generation:<id>…" : activePage === "jobs" ? "user:<id>, project:<id>, provider:<name>…" : "Email, ID, model, project…";
  const hasDateFilter = ["credits", "jobs", "audit"].includes(activePage);
  $("adminDateFromField").hidden = !hasDateFilter;
  $("adminDateToField").hidden = !hasDateFilter;
  $("adminDateFrom").value = "";
  $("adminDateTo").value = "";
  $("adminStatusFilter").innerHTML = '<option value="">All statuses</option>' + (config.statuses || []).map((item) => `<option>${esc(item)}</option>`).join("");
  document.querySelectorAll("[data-admin-nav]").forEach((link) => link.classList.toggle("active", link.dataset.adminNav === activePage));
  $("adminContent").innerHTML = '<div class="admin-loading">Loading operational data…</div>';
  closeDrawer();
  try {
    const payload = await request(`${config.endpoint}${queryFor(activePage)}`);
    $("adminAsOf").textContent = `AS OF ${date(payload.as_of || payload.checked_at || new Date())}`;
    render(activePage, payload);
    if (parsed.id) await openDetail(activePage, parsed.id);
  } catch (error) {
    $("adminContent").innerHTML = `<div class="admin-error">${esc(error.message)}</div>`;
  }
}

function render(page, data) {
  const renderers = { overview: renderOverview, users: renderUsers, credits: renderCredits, models: renderModels, providers: renderProviders, routing: renderRouting, jobs: renderJobs, projects: renderProjects, system: renderSystem, audit: renderAudit };
  $("adminContent").innerHTML = renderers[page](data);
}

function metric(label, value, note = "") {
  return `<article class="admin-metric"><span>${esc(label)}</span><strong>${esc(fmt(value))}</strong><small>${esc(note)}</small></article>`;
}

function table(headers, rows, empty = "No records match this view.") {
  if (!rows.length) return `<div class="admin-empty">${esc(empty)}</div>`;
  return `<div class="admin-section"><div class="admin-table-wrap"><table class="admin-table"><thead><tr>${headers.map((item) => `<th>${esc(item)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div></div>`;
}

function renderOverview(data) {
  const today = data.jobs_today || {};
  const week = data.jobs_7d || {};
  const providers = data.providers || {};
  return `<div class="admin-metrics">
    ${metric("Total users", data.users?.total, `${fmt(data.users?.active)} active`)}
    ${metric("Jobs today", today.total, `${fmt(today.statuses?.RUNNING || 0)} running · ${fmt(today.statuses?.FAILED || 0)} failed`)}
    ${metric("7-day success", week.success_rate == null ? "—" : `${(week.success_rate * 100).toFixed(1)}%`, week.rate_coverage)}
    ${metric("Credits used · 7d", data.credits_consumed_7d, "Settled only")}
    ${metric("Revenue · 7d", `${Number(data.revenue_7d?.amount_usdc || 0).toFixed(2)} USDC`, data.revenue_7d?.coverage)}
    ${metric("Live models", data.live_models, "Lifecycle = LIVE")}
    ${metric("Healthy providers", providers.HEALTHY || 0, `${fmt(providers.NOT_CONFIGURED || 0)} not configured`)}
    ${metric("Provider incidents", (providers.DOWN || 0) + (providers.DEGRADED || 0), "Down + degraded")}
  </div>${table(["Job", "Provider / model", "Error", "Created"], (data.recent_errors || []).map((job) => `<tr data-detail="job" data-id="${esc(job.id)}" tabindex="0"><td class="admin-mono">${esc(job.id)}</td><td>${esc(job.provider)} · ${esc(job.model)}</td><td>${status(job.error_code || "FAILED")} ${esc(job.error_message || "")}</td><td>${date(job.created_at)}</td></tr>`), "No recent failed jobs.")}`;
}

function renderUsers(data) {
  return table(["User", "Role", "Plan", "Status", "Available / reserved", "Generations", "Last activity"], (data.items || []).map((item) => `<tr data-detail="user" data-id="${esc(item.id)}" tabindex="0"><td><strong>${esc(item.email)}</strong><br><span class="admin-mono admin-muted">${esc(item.id)}</span></td><td>${status(item.platform_role)}</td><td>${esc(item.plan || "—")}</td><td>${status(item.status)}</td><td class="admin-mono">${fmt(item.credits_balance)} / ${fmt(item.reserved_credits)}</td><td class="admin-mono">${fmt(item.generation_count)}</td><td>${date(item.last_activity)}</td></tr>`));
}

function renderCredits(data) {
  const summary = data.summary || {};
  return `<section class="admin-metrics admin-metrics-4">
    ${metric("Available", summary.available)}
    ${metric("Held", summary.held)}
    ${metric("Deducted", summary.deducted)}
    ${metric("Released", summary.released)}
  </section>${table(["Source / event", "Workspace", "Generation", "Credits", "Balance Δ / after", "Reason", "Time"], (data.items || []).map((item) => `<tr data-detail="job" data-id="${esc(item.generation_job_id || "")}" tabindex="0"><td><span class="admin-muted">${esc(item.source || "GENERATION_LIFECYCLE")}</span><br>${status(item.event_type)}</td><td class="admin-mono">${esc(item.workspace_id)}</td><td class="admin-mono">${esc(item.generation_job_id || "—")}</td><td>${fmt(item.credits)}</td><td class="admin-mono">${fmt(item.balance_delta)} / ${fmt(item.balance_after)}</td><td>${esc(item.reason)}</td><td>${date(item.created_at)}</td></tr>`))}`;
}

function renderModels(data) {
  return table(["Model", "Provider", "Capability", "Lifecycle", "Configured / verified", "Router", "Pricing", "Live canary", "Last live test"], (data.items || []).map((item) => `<tr data-detail="model" data-id="${esc(item.id)}" tabindex="0"><td><strong>${esc(item.display_name)}</strong><br><span class="admin-mono admin-muted">${esc(item.internal_key)}</span></td><td>${esc(item.provider)}</td><td>${esc(item.capability)}<br><span class="admin-muted">${esc((item.generation_modes || []).join(", "))}</span></td><td>${status(item.lifecycle_status)}</td><td>${item.configured ? "Yes" : "No"} / ${item.verified ? "Yes" : "No"}</td><td>${status(item.router_enabled ? "ACTIVE" : "DISABLED")}</td><td class="admin-mono">${esc(item.cost_class)}</td><td>${status(item.live_canary_status)}</td><td>${date(item.last_live_test_at)}</td></tr>`));
}

function renderProviders(data) {
  const items = data.items || [];
  if (!items.length) return '<div class="admin-empty">No providers are registered.</div>';
  return `<div class="admin-card-grid">${items.map((item) => `<article class="admin-provider-card"><header><strong>${esc(item.name)}</strong>${status(item.health)}</header><dl><dt>Enabled</dt><dd>${item.enabled ? "YES" : "NO"}</dd><dt>Credential present</dt><dd>${item.credential_present ? "YES" : "NO"}</dd><dt>Models</dt><dd>${fmt(item.registered_models?.length || 0)}</dd><dt>Error rate</dt><dd>${item.error_rate == null ? "UNKNOWN" : `${(item.error_rate * 100).toFixed(1)}%`}</dd><dt>Last success</dt><dd>${date(item.last_successful_probe)}</dd></dl><p class="admin-muted">${esc(item.detail || "")}</p><div class="admin-actions"><button class="btn btn-secondary" data-provider-probe="${esc(item.name)}">Metadata probe</button>${currentUser()?.platform_role === "SUPER_ADMIN" ? `<button class="btn ${item.enabled ? "btn-danger" : "btn-secondary"}" data-provider-toggle="${esc(item.name)}" data-enabled="${item.enabled}">${item.enabled ? "Disable" : "Enable"}</button>` : ""}</div></article>`).join("")}</div>`;
}

function renderRouting(data) {
  return `<p class="admin-muted">Router version <span class="admin-mono">${esc(data.router_version)}</span>. Scores are intentionally not fabricated without request requirements.</p>${table(["Model", "Lifecycle", "Router", "Eligible", "Evidence", "Score coverage"], (data.items || []).map((item) => `<tr data-detail="model" data-id="${esc(item.id)}" tabindex="0"><td>${esc(item.provider)} · ${esc(item.internal_key)}</td><td>${status(item.lifecycle_status)}</td><td>${status(item.router_enabled ? "ACTIVE" : "DISABLED")}</td><td>${status(item.router_eligible ? "ACTIVE" : "DISABLED")}</td><td>${fmt(item.evidence?.length || 0)} records</td><td class="admin-muted">${esc(item.score_coverage)}</td></tr>`))}`;
}

function renderJobs(data) {
  return table(["Job", "User / project", "Status", "Provider / model", "Duration", "Credits", "Retry", "Created"], (data.items || []).map((item) => `<tr data-detail="job" data-id="${esc(item.id)}" tabindex="0"><td class="admin-mono">${esc(item.id)}</td><td>${esc(item.user?.email || "—")}<br><span class="admin-muted">${esc(item.project?.title || item.project_id)}</span></td><td>${status(item.status)}</td><td>${esc(item.provider)} · ${esc(item.model)}</td><td>${item.duration_seconds == null ? "—" : `${item.duration_seconds.toFixed(1)}s`}</td><td>${fmt(item.credits)}</td><td>${fmt(item.retry_count)}</td><td>${date(item.created_at)}</td></tr>`));
}

function renderProjects(data) {
  return table(["Project", "Owner", "Status", "Workspace", "Created"], (data.items || []).map((item) => `<tr data-detail="project" data-id="${esc(item.id)}" tabindex="0"><td><strong>${esc(item.title)}</strong><br><span class="admin-mono admin-muted">${esc(item.id)}</span></td><td>${esc(item.owner?.email || "—")}</td><td>${status(item.status)}</td><td class="admin-mono">${esc(item.workspace_id || "—")}</td><td>${date(item.created_at)}</td></tr>`));
}

function renderSystem(data) {
  return `<div class="admin-section">${(data.components || []).map((item) => `<div class="admin-health-row"><div><strong>${esc(item.name)}</strong><br><span class="admin-muted">${esc(item.detail || "No detail")}</span></div><div>${status(item.status)}<br><span class="admin-mono admin-muted">${date(item.last_checked_at)}</span></div></div>`).join("")}</div>`;
}

function renderAudit(data) {
  return table(["Action", "Actor", "Entity", "Reason", "Request", "Created"], (data.items || []).map((item) => `<tr data-detail="audit" data-id="${esc(item.id)}" tabindex="0"><td>${status(item.action)}</td><td>${esc(item.actor_role)}<br><span class="admin-mono admin-muted">${esc(item.actor_user_id)}</span></td><td>${esc(item.entity_type)} · <span class="admin-mono">${esc(item.entity_id)}</span></td><td>${esc(item.reason || "—")}</td><td class="admin-mono">${esc(item.request_id)}</td><td>${date(item.created_at)}</td></tr>`));
}

// The row whose drawer is open is the only selected state this console has, so
// it is marked here and not in the click handler: a deep link (/admin/users/<id>)
// opens the same drawer with no click at all. Called with no arguments it clears
// the mark, because dataset.detail is always a string and never matches null.
function markCurrentRow(kind = null, id = null) {
  document.querySelectorAll(".admin-table tbody tr[data-detail]").forEach((row) => {
    row.classList.toggle("is-current", row.dataset.detail === kind && row.dataset.id === id);
  });
}

async function openDetail(kind, id) {
  if (!id) return;
  markCurrentRow(kind, id);
  const endpoint = { user: `/admin/users/${id}`, model: `/admin/models/${id}`, job: `/admin/jobs/${id}`, project: `/admin/projects/${id}` }[kind];
  if (!endpoint) {
    const audit = (await request(`/admin/audit?entity_id=${encodeURIComponent(id)}`)).items?.[0];
    if (audit) showDrawer(`${audit.action}`, audit);
    return;
  }
  try {
    const data = await request(endpoint);
    showDrawer(kind === "user" ? data.account?.email : data.display_name || data.title || data.id, data, kind);
    const path = kind === "user" ? `/admin/users/${id}` : kind === "model" ? `/admin/models/${id}` : activePath;
    if (path !== location.pathname && ["user", "model"].includes(kind)) history.replaceState({}, "", path);
  } catch (error) {
    showDrawer("Unable to load", { error: error.message });
  }
}

function showDrawer(title, data, kind = "") {
  activeDetail = { kind, data };
  $("adminDrawerTitle").textContent = title || "Record";
  const actions = detailActions(kind, data);
  $("adminDrawerBody").innerHTML = `${actions}<dl class="admin-kv">${Object.entries(flatFacts(data)).map(([key, value]) => `<dt>${esc(key)}</dt><dd>${typeof value === "object" ? `<pre class="admin-json">${esc(JSON.stringify(value, null, 2))}</pre>` : esc(value)}</dd>`).join("")}</dl>`;
  $("adminDrawer").hidden = false;
}

function flatFacts(data) {
  const result = {};
  Object.entries(data || {}).forEach(([key, value]) => {
    if (["password_hash", "secret", "secret_ciphertext"].includes(key)) return;
    result[key.replaceAll("_", " ")] = value == null ? "—" : typeof value === "string" && value.includes("T") && value.endsWith("Z") ? date(value) : value;
  });
  return result;
}

function detailActions(kind, data) {
  if (kind === "job") {
    const allowed = data.allowed_actions || {};
    return `<div class="admin-actions">${allowed.retry ? `<button class="btn btn-secondary" data-job-action="retry" data-id="${esc(data.id)}">Retry job</button>` : ""}${allowed.cancel ? `<button class="btn btn-danger" data-job-action="cancel" data-id="${esc(data.id)}">Cancel job</button>` : ""}</div>`;
  }
  if (kind === "model") {
    const transitions = { DISABLED: ["CONFIGURED"], CONFIGURED: ["TESTING", "DISABLED", "BLOCKED"], TESTING: ["CONFIGURED", "BLOCKED"], VERIFIED: ["LIVE", "TESTING", "DISABLED", "BLOCKED"], LIVE: ["DEGRADED", "DISABLED", "BLOCKED"], DEGRADED: ["LIVE", "DISABLED", "BLOCKED"], BLOCKED: ["CONFIGURED", "DISABLED"] }[data.lifecycle_status] || [];
    const edit = currentUser()?.platform_role === "SUPER_ADMIN" ? `<button class="btn btn-secondary" data-model-verify data-id="${esc(data.id)}">Record completed live validation</button><button class="btn btn-secondary" data-model-edit="metadata" data-id="${esc(data.id)}">Edit metadata &amp; pricing</button><button class="btn btn-secondary" data-model-edit="capabilities" data-id="${esc(data.id)}">Edit capabilities</button>` : "";
    return `<div class="admin-actions">${transitions.map((target) => `<button class="btn ${target === "BLOCKED" ? "btn-danger" : "btn-secondary"}" data-model-transition="${target}" data-id="${esc(data.id)}" data-current="${esc(data.lifecycle_status)}">Move to ${target}</button>`).join("")}<button class="btn ${data.router_enabled ? "btn-danger" : "btn-secondary"}" data-router-toggle="${data.router_enabled}" data-id="${esc(data.id)}">${data.router_enabled ? "Remove from router" : "Restore to router"}</button>${edit}</div>`;
  }
  if (kind === "user") {
    const account = data.account || {};
    const workspace = data.plan?.[0];
    const isSuper = currentUser()?.platform_role === "SUPER_ADMIN";
    const canChangeStatus = isSuper || account.platform_role === "USER";
    const statusButton = canChangeStatus ? `<button class="btn ${account.status === "ACTIVE" ? "btn-danger" : "btn-secondary"}" data-user-status="${account.status === "ACTIVE" ? "SUSPENDED" : "ACTIVE"}" data-id="${esc(account.id)}">${account.status === "ACTIVE" ? "Suspend user" : "Unsuspend user"}</button>` : "";
    const creditButton = isSuper && workspace ? `<button class="btn btn-secondary" data-credit-adjust data-id="${esc(account.id)}" data-workspace="${esc(workspace.workspace_id)}">Adjust credits</button>` : "";
    const planButton = isSuper && workspace ? `<button class="btn btn-secondary" data-plan-change data-id="${esc(account.id)}" data-workspace="${esc(workspace.workspace_id)}" data-current="${esc(workspace.plan_tier)}">Change plan</button>` : "";
    const roleButton = isSuper ? `<button class="btn btn-secondary" data-role-change data-id="${esc(account.id)}" data-current="${esc(account.platform_role)}">Change platform role</button>` : "";
    return `<div class="admin-actions">${statusButton}${creditButton}${planButton}${roleButton}</div>`;
  }
  return "";
}

function closeDrawer() {
  $("adminDrawer").hidden = true;
  activeDetail = null;
  markCurrentRow();
  if (/^\/admin\/(users|models)\/[^/]+/.test(location.pathname)) history.replaceState({}, "", `/admin/${activePage}`);
}

const csv = (value) => String(value || "").split(",").map((item) => item.trim()).filter(Boolean);

function editModelMetadata(model) {
  const displayName = prompt("Display name:", model.display_name || model.internal_key);
  if (displayName == null) return;
  const visibility = prompt("User visible? Enter yes or no:", model.user_visible ? "yes" : "no");
  if (visibility == null || !["yes", "no"].includes(visibility.trim().toLowerCase())) return;
  const existing = model.user_pricing || {};
  const billingUnit = prompt("Billing unit (GENERATION, SECOND, IMAGE, 1K_TOKENS):", existing.billing_unit || "GENERATION");
  if (billingUnit == null || !["GENERATION", "SECOND", "IMAGE", "1K_TOKENS"].includes(billingUnit.trim().toUpperCase())) return;
  const credits = Number(prompt("User credits per billing unit:", existing.credits ?? 0));
  const amount = Number(prompt("Displayed currency amount per unit:", existing.amount ?? 0));
  const currency = prompt("Displayed currency (USD, USDC, CREDITS):", existing.currency || "CREDITS");
  if (!Number.isFinite(credits) || credits < 0 || !Number.isFinite(amount) || amount < 0 || currency == null || !["USD", "USDC", "CREDITS"].includes(currency.trim().toUpperCase())) return;
  confirmAction({
    title: `Update ${model.internal_key} metadata and pricing`,
    description: "This changes user-facing model metadata and pricing. It does not automatically make the model LIVE.",
    run: (reason) => request(`/admin/models/${model.id}/metadata`, { method: "POST", body: JSON.stringify({ display_name: displayName.trim(), user_visible: visibility.trim().toLowerCase() === "yes", pricing_metadata: { billing_unit: billingUnit.trim().toUpperCase(), credits, currency: currency.trim().toUpperCase(), amount }, reason }) }),
  });
}

function editModelCapabilities(model) {
  const profile = model.capabilities || {};
  const operations = prompt("Supported operations (comma separated):", (profile.supported_operations || []).join(", "));
  if (operations == null || !csv(operations).length) return;
  const currentModes = [["IMAGE_GENERATION", profile.supports_image_generation], ["VIDEO_GENERATION", profile.supports_video_generation], ["T2V", profile.supports_t2v], ["I2V", profile.supports_i2v], ["V2V", profile.supports_v2v], ["REFERENCE_IMAGE", profile.supports_reference_image], ["MULTI_REFERENCE", profile.supports_multi_reference], ["START_FRAME", profile.supports_start_frame], ["END_FRAME", profile.supports_end_frame], ["AUDIO", profile.supports_audio]].filter(([, enabled]) => enabled).map(([name]) => name);
  const rawModes = prompt("Capability flags (comma separated): IMAGE_GENERATION, VIDEO_GENERATION, T2V, I2V, V2V, REFERENCE_IMAGE, MULTI_REFERENCE, START_FRAME, END_FRAME, AUDIO", currentModes.join(", "));
  if (rawModes == null) return;
  const modes = new Set(csv(rawModes).map((item) => item.toUpperCase()));
  const allowed = new Set(["IMAGE_GENERATION", "VIDEO_GENERATION", "T2V", "I2V", "V2V", "REFERENCE_IMAGE", "MULTI_REFERENCE", "START_FRAME", "END_FRAME", "AUDIO"]);
  if ([...modes].some((item) => !allowed.has(item))) return alert("Unknown capability flag.");
  const maxReferences = Number(prompt("Maximum reference images:", profile.max_reference_images ?? 0));
  const minDurationRaw = prompt("Minimum duration in seconds (blank for none):", profile.min_duration ?? "");
  const maxDurationRaw = prompt("Maximum duration in seconds (blank for none):", profile.max_duration ?? "");
  if (!Number.isInteger(maxReferences) || maxReferences < 0 || maxReferences > 16) return;
  const minDuration = minDurationRaw === "" ? null : Number(minDurationRaw);
  const maxDuration = maxDurationRaw === "" ? null : Number(maxDurationRaw);
  if ((minDuration != null && (!Number.isFinite(minDuration) || minDuration <= 0)) || (maxDuration != null && (!Number.isFinite(maxDuration) || maxDuration <= 0))) return;
  const aspectRatios = prompt("Supported aspect ratios (comma separated):", (profile.supported_aspect_ratios || []).join(", "));
  const resolutions = prompt("Supported resolutions (comma separated):", (profile.supported_resolutions || []).join(", "));
  if (aspectRatios == null || resolutions == null) return;
  const enabled = (name) => modes.has(name);
  confirmAction({
    title: `Replace ${model.internal_key} capability contract`,
    description: "Capability changes invalidate prior verification, return the model to CONFIGURED, and remove it from automatic routing until it is tested and promoted again.",
    run: (reason) => request(`/admin/models/${model.id}/capabilities`, { method: "POST", body: JSON.stringify({ supported_operations: csv(operations), supports_image_generation: enabled("IMAGE_GENERATION"), supports_video_generation: enabled("VIDEO_GENERATION"), supports_t2v: enabled("T2V"), supports_i2v: enabled("I2V"), supports_v2v: enabled("V2V"), supports_reference_image: enabled("REFERENCE_IMAGE"), supports_multi_reference: enabled("MULTI_REFERENCE"), supports_start_frame: enabled("START_FRAME"), supports_end_frame: enabled("END_FRAME"), supports_audio: enabled("AUDIO"), max_reference_images: maxReferences, min_duration: minDuration, max_duration: maxDuration, supported_aspect_ratios: csv(aspectRatios), supported_resolutions: csv(resolutions), reason }) }),
  });
}

function confirmAction({ title, description, danger = true, reason = true, run }) {
  pendingConfirm = run;
  $("adminConfirmTitle").textContent = title;
  $("adminConfirmDescription").textContent = description;
  $("adminReasonField").hidden = !reason;
  $("adminReason").value = "";
  $("adminConfirmSubmit").className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
  $("adminConfirmDialog").showModal();
}

async function executeConfirm() {
  if (!pendingConfirm) return;
  const reason = $("adminReason").value.trim();
  if (!$("adminReasonField").hidden && reason.length < 8) return;
  const run = pendingConfirm;
  pendingConfirm = null;
  $("adminConfirmDialog").close();
  await run(reason);
  await load(`/admin/${activePage === "overview" ? "" : activePage}`.replace(/\/$/, ""));
}

document.addEventListener("click", async (event) => {
  const detail = event.target.closest("[data-detail]");
  if (detail) return openDetail(detail.dataset.detail, detail.dataset.id);
  const probe = event.target.closest("[data-provider-probe]");
  if (probe) {
    probe.disabled = true;
    try { const result = await request(`/admin/providers/${probe.dataset.providerProbe}/probe`, { method: "POST", body: "{}" }); alert(`${result.status}: ${result.detail || "No detail"}`); } catch (error) { alert(error.message); }
    finally { probe.disabled = false; }
    return;
  }
  const provider = event.target.closest("[data-provider-toggle]");
  if (provider) return confirmAction({ title: `${provider.dataset.enabled === "true" ? "Disable" : "Enable"} ${provider.dataset.providerToggle}`, description: "This changes the provider kill switch for new generation traffic. Existing submitted jobs are not cancelled.", run: (reason) => request(`/admin/providers/${provider.dataset.providerToggle}/enablement`, { method: "POST", body: JSON.stringify({ enabled: provider.dataset.enabled !== "true", reason }) }) });
  const transition = event.target.closest("[data-model-transition]");
  if (transition) return confirmAction({ title: `Move model to ${transition.dataset.modelTransition}`, description: `Move this model from ${transition.dataset.current} to ${transition.dataset.modelTransition}. LIVE is separately gated by provider configuration and successful verification.`, danger: ["BLOCKED", "DISABLED"].includes(transition.dataset.modelTransition), run: (reason) => request(`/admin/models/${transition.dataset.id}/lifecycle-transition`, { method: "POST", body: JSON.stringify({ target_status: transition.dataset.modelTransition, reason }) }) });
  const router = event.target.closest("[data-router-toggle]");
  if (router) return confirmAction({ title: router.dataset.routerToggle === "true" ? "Remove model from router" : "Restore model to router", description: "This affects only new automatic selections; already submitted jobs continue under their existing policy.", run: (reason) => request(`/admin/models/${router.dataset.id}/router`, { method: "POST", body: JSON.stringify({ enabled: router.dataset.routerToggle !== "true", reason }) }) });
  const modelEdit = event.target.closest("[data-model-edit]");
  if (modelEdit && activeDetail?.kind === "model") return modelEdit.dataset.modelEdit === "metadata" ? editModelMetadata(activeDetail.data) : editModelCapabilities(activeDetail.data);
  const modelVerify = event.target.closest("[data-model-verify]");
  if (modelVerify && activeDetail?.kind === "model") {
    const jobId = prompt("Completed production generation job ID used as live evidence:")?.trim();
    if (!jobId) return;
    const protocolVersion = prompt("Production protocol version:", "generation-v1")?.trim();
    if (!protocolVersion) return;
    return confirmAction({ title: `Verify ${activeDetail.data.internal_key} from completed job`, description: `The server will reject job ${jobId} unless it is COMPLETED and matches this exact provider/model mapping. This records evidence only; LIVE remains a separate transition.`, danger: false, run: (reason) => request(`/admin/models/${activeDetail.data.id}/verifications`, { method: "POST", headers: { "Idempotency-Key": `live-verification:${jobId}` }, body: JSON.stringify({ protocol_version: protocolVersion, result: "SUCCESS", evidence_reference: `generation-job:${jobId}`, billable: true, detail: reason }) }) });
  }
  const job = event.target.closest("[data-job-action]");
  if (job) return confirmAction({ title: `${job.dataset.jobAction === "retry" ? "Retry" : "Cancel"} generation job`, description: "The existing Gateway state machine will validate this command. Billing and idempotency rules are not bypassed.", run: (reason) => request(`/admin/jobs/${job.dataset.id}/${job.dataset.jobAction}`, { method: "POST", body: JSON.stringify({ reason }) }) });
  const user = event.target.closest("[data-user-status]");
  if (user) return confirmAction({ title: `${user.dataset.userStatus === "SUSPENDED" ? "Suspend" : "Unsuspend"} user`, description: user.dataset.userStatus === "SUSPENDED" ? "All active sessions will be revoked immediately." : "The user will be able to authenticate again.", run: (reason) => request(`/admin/users/${user.dataset.id}/status`, { method: "POST", body: JSON.stringify({ status: user.dataset.userStatus, reason }) }) });
  const plan = event.target.closest("[data-plan-change]");
  if (plan) {
    const target = prompt("Target plan (FREE, PRO, ENTERPRISE):", plan.dataset.current)?.trim().toUpperCase();
    if (!target || !["FREE", "PRO", "ENTERPRISE"].includes(target) || target === plan.dataset.current) return;
    return confirmAction({ title: `Change plan from ${plan.dataset.current} to ${target}`, description: "This changes the workspace entitlement tier. Existing submitted jobs are not rewritten.", run: (reason) => request(`/admin/users/${plan.dataset.id}/plan`, { method: "POST", body: JSON.stringify({ workspace_id: plan.dataset.workspace, plan_tier: target, reason }) }) });
  }
  const role = event.target.closest("[data-role-change]");
  if (role) {
    const target = prompt("Target platform role (USER, ADMIN, SUPER_ADMIN):", role.dataset.current)?.trim().toUpperCase();
    if (!target || !["USER", "ADMIN", "SUPER_ADMIN"].includes(target) || target === role.dataset.current) return;
    return confirmAction({ title: `Change platform role from ${role.dataset.current} to ${target}`, description: "This changes access to the platform Admin Console. You cannot demote your own SUPER_ADMIN account.", run: (reason) => request(`/admin/users/${role.dataset.id}/platform-role`, { method: "POST", body: JSON.stringify({ role: target, reason }) }) });
  }
  const credit = event.target.closest("[data-credit-adjust]");
  if (credit) {
    const raw = prompt("Credit delta (positive grant or negative debit):");
    const delta = Number(raw);
    if (!Number.isInteger(delta) || delta === 0) return;
    return confirmAction({ title: `Adjust credits by ${delta}`, description: `Workspace ${credit.dataset.workspace}. The balance cannot become negative and the request is idempotent.`, run: (reason) => request(`/admin/users/${credit.dataset.id}/credit-adjustments`, { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ workspace_id: credit.dataset.workspace, delta, reason }) }) });
  }
});

// The drill-down rows carry tabindex="0" but deliberately NOT role="button".
// role="button" would make a <tr> stop exposing `row`, which orphans every <td>
// (the `cell` role requires an owning `row`), and because button is
// Children-Presentational it would flatten all eight cells into one run-on
// accessible name — losing column-header association in every admin table. So
// the row stays a row and this handler supplies by hand the Enter/Space that a
// real button would have given for free. Until it existed every detail view in
// the console was mouse-only, which made the whole console unusable from the
// keyboard.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const row = e.target.closest?.(".admin-table tbody tr[data-detail]");
  if (!row) return;
  e.preventDefault();
  row.click();
});

$("adminRefreshBtn").addEventListener("click", () => load(activePath));
$("adminApplyFilters").addEventListener("click", async () => {
  const config = PAGE[activePage];
  $("adminContent").innerHTML = '<div class="admin-loading">Applying filters…</div>';
  try { render(activePage, await request(`${config.endpoint}${queryFor(activePage)}`)); } catch (error) { $("adminContent").innerHTML = `<div class="admin-error">${esc(error.message)}</div>`; }
});
$("adminDrawerClose").addEventListener("click", closeDrawer);
$("adminConfirmSubmit").addEventListener("click", (event) => { event.preventDefault(); executeConfirm().catch((error) => alert(error.message)); });

onRoute((route, user) => {
  if (!isAdminRoute(route)) return;
  if (!["ADMIN", "SUPER_ADMIN"].includes(user?.platform_role)) return navigate("/app", { replace: true });
  $("adminIdentity").textContent = `${user.email} · ${user.platform_role}`;
  load(route);
});
