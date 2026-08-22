const API = window.AI_DIRECTOR_API || (location.port === "3000" ? "/api" : "http://127.0.0.1:18080");
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
  selectedCharacterId: null, mode: "passenger", passengerMedia: "image", passengerOriginal: null,
  passengerPrompts: { image: "", video: "" }, passengerJobs: { image: null, video: null },
  passengerReferenceUpload: null, modelProfiles: [], imageModelProfiles: [], passengerModels: [],
  confirmedAssets: new Set(), logicalAssets: [],
  authUser: null,
  authMode: "login", passengerPreviewObjectUrl: null,
  styleLock: null,
  submissions: restoreSubmissions(),
};
const $ = (id) => document.getElementById(id);
const escapeHTML = (value = "") => String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
const simpleLabel = (value) => ({
  NEW: "等待排队", RESERVED: "已分配资源", DRAFT: "草稿", PLANNED: "已规划", READY: "可生成",
  COMPILED: "已拆解", ACTION: "动作镜头", DIALOGUE: "对话镜头", MEDIUM: "中景",
  CLOSE_UP: "近景", WIDE: "全景", EXTREME_CLOSE_UP: "特写", COMMERCIAL_HERO: "商业主画面",
  QUEUED: "排队中", SUBMITTED: "已提交", RUNNING: "生成中", RETRY_WAIT: "等待重试",
  COMPLETED: "已完成", CANCELLED: "已取消", WORKER_NEEDS_USER_ACTION: "需要你的操作",
  GENERATING: "生成中",
  VALIDATING: "检查中", PASSED: "检查通过", SOFT_FAILED: "建议修复", HARD_FAILED: "未通过",
  PASS: "检查通过", SOFT_FAIL: "建议修复", HARD_FAIL: "未通过",
  USER_REVIEW_REQUIRED: "需要人工确认", COMMITTED: "已采用", REJECTED: "未采用", FAILED: "失败",
  NONE: "独立镜头", PREVIOUS_END_FRAME: "接续上一画面", REFERENCE_FRAME: "使用参考画面",
  HARD_CONTINUITY: "紧接上一镜", HYBRID: "尾帧加参考图", RE_ANCHOR: "重新固定人物与场景",
  CAMERA_AXIS_CHANGE: "可能越过画面方向线", SCENE_CHANGE: "场景发生变化", TIMELINE_JUMP: "时间有跳跃",
  LOW_PREVIOUS_FRAME_QUALITY: "上一镜尾帧清晰度不足", LOW_PREVIOUS_FACE_VISIBILITY: "上一镜人物面部不清",
  IDENTITY_DRIFT_RISK: "人物外观可能变化", ACTION_DISCONTINUITY: "动作衔接不顺",
  HIGH_CONTINUITY_RISK: "衔接风险较高", SAME_SCENE: "同一场景", ACTION_CHAIN_CONTINUES: "动作连续",
  USABLE_END_FRAME: "上一镜尾帧可用", MODERATE_CAMERA_OR_BLOCKING_CHANGE: "机位或人物位置变化较大",
  TEXT_TO_VIDEO: "文字生成视频", IMAGE_TO_VIDEO: "图片生成视频", CONTINUE_I2V: "沿用上一镜继续生成",
  CONTINUE_V2V: "接续上一段视频", HYBRID_REFERENCE: "尾帧与参考图结合",
  REANCHOR_CHARACTER: "重新固定人物", REANCHOR_SCENE: "重新固定场景", REANCHOR_FULL: "重新固定人物与场景",
  START_END_FRAME: "指定首尾画面", REFERENCE_TO_VIDEO: "参考图生成视频",
  portrait: "人像", beauty_fashion: "美妆时尚", product: "产品",
  commercial: "商业广告", scene_concept: "场景概念", reference_character_regeneration: "人物身份保护",
  CHARACTER: "人物", SCENE: "场景", PRODUCT: "产品", PROP: "道具", WARDROBE: "服装",
  VEHICLE: "载具", CREATURE: "生物", VOICE: "声音", STYLE: "视觉风格", REFERENCE: "普通参考",
  google_flow: "Google Flow", seedance: "Seedance", veo_official: "Veo", grok: "Grok",
  kling: "可灵", runway: "Runway", omni: "Omni", wan: "Wan",
}[value] || value || "—");

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
  const response = await fetch(`${API}${path}`, {
    ...options,
    credentials: "include",
    headers,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    if (response.status === 401 && !path.startsWith("/api/auth/")) lockAuth();
    throw new Error(detail.detail || `请求失败 (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}

function lockAuth() {
  if (state.passengerPreviewObjectUrl) URL.revokeObjectURL(state.passengerPreviewObjectUrl);
  state.passengerPreviewObjectUrl = null;
  state.authUser = null;
  clearWorkspaceState();
  $("authGate").classList.remove("hidden");
  $("appShell").classList.add("auth-locked");
  setAuthMode("login");
  $("authPassword").value = "";
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
  sessionStorage.removeItem(SUBMISSION_STORAGE_KEY);
  state.confirmedAssets.clear();
  $("projectSelect").innerHTML = '<option value="">尚无项目</option>';
  $("characterList").innerHTML = "<p>尚未建立角色</p>";
  $("passengerExistingAsset").innerHTML = '<option value="">新建一个素材</option>';
  $("manualExistingAsset").innerHTML = '<option value="">新建一个素材</option>';
  $("manualAssetFile").value = "";
  $("manualAssetStatus").textContent = "人物主参考图也可以在“智能导演 → 人物”中直接更新。";
  $("lockProjectStyleBtn").disabled = true;
  $("projectStyleLockStatus").textContent = "请先把一版视觉风格设为正式参考，再由项目成员明确锁定。";
  $("passengerReference").value = "";
  $("passengerPrompt").value = "";
  $("scriptInput").value = "";
  $("rawPrompt").value = "";
  $("compiledPrompt").value = "";
  resetProductionView();
  $("passengerResult").classList.add("empty-state");
  $("passengerResult").textContent = "提交生成后在这里查看状态";
}

function unlockAuth(user) {
  state.authUser = user;
  $("accountName").textContent = user.display_name || user.email;
  $("authGate").classList.add("hidden");
  $("appShell").classList.remove("auth-locked");
}

function setAuthMode(mode) {
  state.authMode = mode;
  const registering = mode === "register";
  $("registerFields").classList.toggle("hidden", !registering);
  $("authTitle").textContent = registering ? "创建你的工作空间" : "登录工作空间";
  $("authDescription").textContent = registering
    ? "注册后会自动创建一个只属于你的工作空间。"
    : "登录后只会看到你所在工作空间的项目与素材。";
  $("authSubmitBtn").textContent = registering ? "注册并进入工作台" : "登录";
  $("authModeBtn").textContent = registering ? "已有账号？返回登录" : "没有账号？免费注册";
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
}

async function startWorkspace() {
  await Promise.all([loadProjects(), loadPassengerModels()]);
}

async function bootstrapAuth() {
  health();
  try {
    const user = await request("/api/auth/me");
    unlockAuth(user);
    await startWorkspace();
  } catch (_error) {
    lockAuth();
  }
}

function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.add("show");
  setTimeout(() => $("toast").classList.remove("show"), 2800);
}

async function health() {
  try {
    await request("/health");
    $("systemStatus").innerHTML = "<i></i>生成系统在线";
  } catch (error) {
    $("systemStatus").innerHTML = "<i></i>API 未连接";
    $("systemStatus").style.color = "#ff7f91";
  }
}

function switchMode(mode) {
  state.mode = mode;
  document.querySelectorAll("[data-mode]").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  $("passengerWorkspace").classList.toggle("hidden", mode !== "passenger");
  $("autopilotWorkspace").classList.toggle("hidden", mode !== "autopilot");
  $("generateBtn").classList.toggle("hidden", mode !== "autopilot");
  $("modeDescription").textContent = mode === "passenger"
    ? "描述画面、选择生成方式、开始创作"
    : "系统拆解剧本、保持连续并检查每个镜头";
}

function setPassengerMedia(media) {
  state.passengerPrompts[state.passengerMedia] = $("passengerPrompt").value;
  state.passengerMedia = media;
  $("passengerPrompt").value = state.passengerPrompts[media];
  document.querySelectorAll("[data-media]").forEach((button) => button.classList.toggle("active", button.dataset.media === media));
  $("passengerDurationField").classList.toggle("hidden", media !== "video");
  $("imagePromptActions").classList.toggle("hidden", media !== "image");
  $("passengerGenerateBtn").textContent = media === "video" ? "开始生成视频" : "开始生成图片";
  $("passengerPromptHeading").textContent = media === "video" ? "描述你想要的视频" : "描述你想要的图片";
  $("passengerReferenceLabel").textContent = media === "video" ? "首帧或人物参考图（可选）" : "参考图片（可选）";
  $("passengerPrompt").placeholder = media === "video"
    ? "例如：人物看向窗外，镜头缓慢推进，全程不看摄影机"
    : "例如：一个女生拿着香水，高级一点";
  $("promptTypeBadge").textContent = media === "video" ? "视频原文" : "图片意图自动识别";
  $("promptCorrectionSummary").textContent = media === "video"
    ? "视频描述不会套用图片优化规则；系统会按你的原意提交。"
    : "只会增强构图、灯光、材质和层次，不会擅自重设计。";
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
    : '<option value="">尚未配置生成模型</option>';
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
  if (!profile) { $("passengerCost").textContent = "选择模型后显示"; return; }
  const estimate = passengerEstimatedCost();
  $("passengerCost").textContent = estimate > 0
    ? `约 ${Math.max(1, Math.ceil(estimate / .01))} 积分 · $${estimate.toFixed(2)}`
    : "当前模型按供应商账户实际结算";
}

function passengerReferenceFingerprint(projectId, file) {
  return JSON.stringify([projectId, file.name, file.size, file.lastModified, file.type]);
}

async function uploadPassengerReference({ projectId, file }) {
  if (!file) return null;
  if (!projectId) throw new Error("请先创建项目");
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
    if (!response.ok) throw new Error("参考图片上传失败");
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
  if (state.passengerMedia !== "image") return toast("画面描述优化仅用于图片；视频会按你的原意提交");
  const prompt = $("passengerPrompt").value.trim();
  if (!prompt) return toast("请先描述想要的画面");
  const projectId = state.project?.id || null;
  const reference = await uploadPassengerReference({
    projectId,
    file: $("passengerReference").files[0],
  });
  const path = projectId ? `/api/prompt/correct?project_id=${projectId}` : "/api/prompt/correct";
  const result = await request(path, {
    method: "POST",
    body: JSON.stringify({ prompt, task_type: "auto", reference_assets: reference ? [reference] : [] }),
  });
  state.passengerOriginal ??= result.original_prompt;
  $("passengerPrompt").value = result.corrected_prompt;
  $("undoImagePromptBtn").disabled = false;
  $("promptTypeBadge").textContent = result.identity_preservation_mode ? "人物身份保护" : simpleLabel(result.detected_type);
  $("promptCorrectionSummary").textContent = `已优化 ${result.changes.length} 处画面细节，并保留 ${result.preserved_constraints.length} 项原始要求。`;
  toast("画面描述已优化，你可以继续修改或恢复原文");
}

function undoPassengerPrompt() {
  if (!state.passengerOriginal) return;
  $("passengerPrompt").value = state.passengerOriginal;
  state.passengerOriginal = null;
  $("undoImagePromptBtn").disabled = true;
  $("promptTypeBadge").textContent = "已恢复";
  $("promptCorrectionSummary").textContent = "已恢复用户原始提示词。";
}

async function renderPassengerJob(job) {
  if (!job) {
    if (state.passengerPreviewObjectUrl) URL.revokeObjectURL(state.passengerPreviewObjectUrl);
    state.passengerPreviewObjectUrl = null;
    $("passengerResult").classList.add("empty-state");
    $("passengerResult").textContent = "提交生成后在这里查看状态";
    $("promotePassengerAssetBtn").disabled = true;
    $("promotePassengerAssetBtn").textContent = "确认用于当前项目";
    return;
  }
  const output = job.output_asset_id || "等待生成完成";
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
      const response = await fetch(`${API}/v1/storage/${storagePath}`, {
        credentials: "include",
      });
      if (response.ok) {
        state.passengerPreviewObjectUrl = URL.createObjectURL(await response.blob());
        mediaUrl = state.passengerPreviewObjectUrl;
      }
    }
    if (mediaUrl && asset.mime_type?.startsWith("image/")) {
      preview = `<img class="result-preview" src="${escapeHTML(mediaUrl)}" alt="生成结果" />`;
    } else if (mediaUrl && asset.mime_type?.startsWith("video/")) {
      preview = `<video class="result-preview" src="${escapeHTML(mediaUrl)}" controls playsinline></video>`;
    }
  }
  const displayedStatus = job.credit_status === "RECONCILIATION_REQUIRED"
    ? "对账中 · 积分暂时冻结"
    : simpleLabel(job.status);
  $("passengerResult").classList.remove("empty-state");
  $("passengerResult").innerHTML = `${preview}<span class="result-status">${displayedStatus}</span><div class="result-meta">
    <div>生成模型<strong>${simpleLabel(job.provider)} · ${escapeHTML(job.model)}</strong></div>
    <div>任务编号<strong>${escapeHTML(job.id)}</strong></div>
    <div>结果素材<strong>${escapeHTML(output)}</strong></div>
  </div>`;
  const confirmed = state.confirmedAssets.has(job.output_asset_id);
  $("promotePassengerAssetBtn").disabled = !job.output_asset_id || confirmed;
  $("promotePassengerAssetBtn").textContent = confirmed ? "已确认为项目素材" : "确认用于当前项目";
}

async function generatePassenger() {
  if (!state.project) return toast("请先创建项目");
  const prompt = $("passengerPrompt").value.trim();
  const selection = selectedPassengerModel();
  if (!prompt || !selection) return toast("请填写提示词并选择模型");
  const projectId = state.project.id;
  const mediaType = state.passengerMedia;
  const aspectRatio = $("passengerAspect").value;
  const resolution = $("passengerResolution").value;
  const duration = mediaType === "video" ? Number($("passengerDuration").value || 4) : null;
  const estimatedCost = passengerEstimatedCost();
  const freeVideo = mediaType === "video"
    && state.authUser?.workspaces?.some((workspace) => workspace.plan_tier === "FREE");
  const file = $("passengerReference").files[0];
  const fingerprint = JSON.stringify({
    projectId, mediaType, provider: selection.provider, model: selection.model_id,
    modelRole: freeVideo ? "VIDEO_SEEDANCE" : null,
    prompt, aspectRatio, resolution, duration, estimatedCost,
    file: file ? [file.name, file.size, file.lastModified] : null,
  });
  const idempotencyKey = beginSubmission("passenger", fingerprint);
  if (!idempotencyKey) return;
  const button = $("passengerGenerateBtn");
  button.disabled = true;
  button.textContent = "正在提交…";
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
    succeeded = true;
    toast(freeVideo
      ? "生成任务已提交；免费套餐使用 Seedance"
      : "生成任务已提交；你选择的模型不会被系统自动替换");
  } finally {
    finishSubmission("passenger", idempotencyKey, succeeded);
    button.disabled = false;
    ["passengerPrompt", "passengerModel", "passengerAspect", "passengerResolution",
      "passengerDuration", "passengerReference"].forEach((id) => { $(id).disabled = false; });
    button.textContent = state.passengerMedia === "video" ? "开始生成视频" : "开始生成图片";
  }
}

async function refreshPassengerJob() {
  const current = state.passengerJobs[state.passengerMedia];
  if (!current) return toast("当前没有生成任务");
  const job = await request(`/v1/generations/${current.id}`);
  state.passengerJobs[state.passengerMedia] = job;
  await renderPassengerJob(job);
}

async function confirmPassengerAsset() {
  const job = state.passengerJobs[state.passengerMedia];
  if (!job?.output_asset_id) return toast("生成完成后才能确认素材");
  const name = $("passengerAssetName").value.trim() || `${state.passengerMedia === "image" ? "图片" : "视频"}素材`;
  const result = await request(`/api/generations/${job.id}/promote`, {
    method: "POST",
    body: JSON.stringify({
      asset_id: $("passengerExistingAsset").value || null,
      asset_type: $("passengerAssetType").value,
      name,
      promote_to_canonical: $("passengerPromoteCanonical").checked,
      reason: $("passengerPromoteCanonical").checked ? "用户在自主创作中明确设为正式参考" : "",
    }),
  });
  state.confirmedAssets.add(job.output_asset_id);
  await loadLogicalAssets();
  await renderPassengerJob(job);
  toast(result.canonical ? "已建立素材版本并设为正式参考" : "已建立项目素材候选版本");
}

async function loadProjects() {
  state.projects = await request("/v1/projects");
  $("projectSelect").innerHTML = state.projects.length
    ? state.projects.map((project) => `<option value="${project.id}">${escapeHTML(project.name)}</option>`).join("")
    : '<option value="">尚无项目</option>';
  if (state.projects.length) await selectProject(state.projects[0].id);
  else clearWorkspaceState();
}

async function loadLogicalAssets() {
  if (!state.project) return;
  [state.logicalAssets, state.styleLock] = await Promise.all([
    request(`/api/projects/${state.project.id}/assets`),
    request(`/api/projects/${state.project.id}/style-lock`),
  ]);
  $("passengerExistingAsset").innerHTML = '<option value="">新建一个素材</option>' + state.logicalAssets
    .map((asset) => `<option value="${asset.id}">${simpleLabel(asset.asset_type)} · ${escapeHTML(asset.name)}${asset.canonical_version_id ? " · 正式版" : ""}</option>`)
    .join("");
  $("manualExistingAsset").innerHTML = '<option value="">新建一个素材</option>' + state.logicalAssets
    .map((asset) => `<option value="${asset.id}">${simpleLabel(asset.asset_type)} · ${escapeHTML(asset.name)}${asset.canonical_version_id ? " · 当前正式参考" : ""}</option>`)
    .join("");
  renderProjectStyleLock();
}

function renderProjectStyleLock() {
  const selected = state.logicalAssets.find((asset) => asset.id === $("manualExistingAsset").value);
  const lockable = selected?.asset_type === "STYLE" && selected.canonical_version_id;
  $("lockProjectStyleBtn").disabled = Boolean(state.styleLock?.locked) || !lockable;
  $("lockProjectStyleBtn").textContent = state.styleLock?.locked ? "整部作品画风已锁定" : "锁定为整部作品画风";
  $("projectStyleLockStatus").textContent = state.styleLock?.locked
    ? `已锁定版本 ${state.styleLock.style_version_id.slice(0, 8)}；后续镜头会自动继承并经过画风漂移检查。`
    : (lockable ? "锁定后不可替换；系统会提取 Style Embedding 并用于全片生成与质量门禁。" : "请选择一项已有的视觉风格正式参考。 ");
}

function syncManualAssetSelection() {
  const selected = state.logicalAssets.find((asset) => asset.id === $("manualExistingAsset").value);
  if (!selected) {
    renderProjectStyleLock();
    return;
  }
  $("manualAssetType").value = selected.asset_type;
  $("manualAssetName").value = selected.name;
  renderProjectStyleLock();
}

async function lockSelectedProjectStyle() {
  if (!state.project) return toast("请先创建项目");
  const selected = state.logicalAssets.find((asset) => asset.id === $("manualExistingAsset").value);
  if (selected?.asset_type !== "STYLE" || !selected.canonical_version_id) {
    return toast("请选择已经设为正式参考的视觉风格");
  }
  if (!window.confirm("画风锁定后不可替换，并会成为所有镜头的生成与采用门禁。确认继续？")) return;
  state.styleLock = await request(`/api/projects/${state.project.id}/style-lock`, {
    method: "POST",
    body: JSON.stringify({
      style_version_id: selected.canonical_version_id,
      reason: "用户在项目素材管理中明确确认整部作品画风",
      explicit_confirmation: true,
    }),
  });
  state.project.canonical_style_version_id = state.styleLock.style_version_id;
  renderProjectStyleLock();
  toast("画风已锁定；所有后续镜头将自动继承并检查漂移");
}

async function uploadManualAssetVersion() {
  if (!state.project) return toast("请先创建项目");
  const file = $("manualAssetFile").files[0];
  if (!file) return toast("请选择修改后的图片");
  const existing = state.logicalAssets.find((asset) => asset.id === $("manualExistingAsset").value);
  const assetType = existing?.asset_type || $("manualAssetType").value;
  const assetName = (existing?.name || $("manualAssetName").value).trim();
  if (!assetName) return toast("请填写素材名称");
  const button = $("manualAssetUploadBtn");
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    const form = new FormData();
    form.append("project_id", state.project.id);
    form.append("asset_type", userUploadMediaType(assetType));
    form.append("file", file);
    const mediaResponse = await fetch(`${API}/v1/assets`, {
      method: "POST",
      body: form,
      credentials: "include",
      headers: csrfHeaders("POST"),
    });
    if (!mediaResponse.ok) {
      const detail = await mediaResponse.json().catch(() => ({}));
      throw new Error(detail.detail || "图片上传失败");
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
        label: `用户上传 v${Date.now()}`,
        source: "USER_UPLOAD",
        status: "READY",
      }),
    });
    let promoted = false;
    if ($("manualPromoteCanonical").checked) {
      await request(`/api/assets/${logical.id}/versions/${version.id}/promote`, {
        method: "POST",
        body: JSON.stringify({ reason: "用户明确设为当前正式参考" }),
      });
      promoted = true;
    }
    await loadLogicalAssets();
    $("manualExistingAsset").value = logical.id;
    renderProjectStyleLock();
    $("manualAssetFile").value = "";
    $("manualAssetStatus").textContent = `已保存 ${simpleLabel(assetType)}“${assetName}”的 v${version.version}${promoted ? "，并设为当前正式参考" : "；当前正式参考未改变"}。`;
    toast("新版本已保存，旧版本仍可追溯");
  } finally {
    button.disabled = false;
    button.textContent = "保存为新版本";
  }
}

async function selectProject(id) {
  if (!id) return;
  if (state.project && state.project.id !== id) {
    state.passengerReferenceUpload = null;
    state.passengerJobs = { image: null, video: null };
    state.confirmedAssets.clear();
    $("passengerReference").value = "";
    renderPassengerJob(null);
  }
  state.project = await request(`/v1/projects/${id}`);
  $("projectSelect").value = id;
  await loadLogicalAssets();
  await loadCharacters();
  if (state.project.episodes.length) await loadEpisode(state.project.episodes[0].id);
  else resetProductionView();
}

function resetProductionView() {
  state.episode = null; state.shot = null; state.candidates = [];
  $("sceneList").innerHTML = "创建第一集并拆解剧本后显示场景";
  $("shotTimeline").innerHTML = "暂无镜头";
  $("candidateGrid").innerHTML = "生成后可对比方案 A / B / C、质量检查与成本";
}

async function loadEpisode(id) {
  state.episode = await request(`/v1/episodes/${id}`);
  $("scriptInput").value = state.episode.script_source || "";
  $("episodeStatus").textContent = simpleLabel(state.episode.status);
  renderScenes(); renderShots();
  const firstShot = state.episode.scenes.flatMap((scene) => scene.shots)[0];
  if (firstShot) await selectShot(firstShot.id);
}

function renderScenes() {
  const scenes = state.episode?.scenes || [];
  $("sceneCount").textContent = scenes.length;
  $("sceneList").classList.toggle("empty-state", !scenes.length);
  $("sceneList").innerHTML = scenes.length ? scenes.map((scene) => `
    <div class="scene-item"><strong>场景 ${String(scene.sequence).padStart(2, "0")} · ${escapeHTML(scene.description)}</strong>
    <small>${escapeHTML(scene.time_context || "时间未指定")} · ${scene.shots.length} 个镜头</small></div>`).join("") : "暂无场景";
}

function renderShots() {
  const shots = state.episode?.scenes.flatMap((scene) => scene.shots) || [];
  $("shotTimeline").classList.toggle("empty-state", !shots.length);
  $("shotTimeline").innerHTML = shots.length ? shots.map((shot, index) => `
    <button class="shot-card ${shot.status === "COMMITTED" ? "committed" : ""} ${state.shot?.id === shot.id ? "active" : ""}" data-shot="${shot.id}">
      <span>镜头 ${String(index + 1).padStart(2, "0")}</span><strong>${escapeHTML(shot.prompt)}</strong>
      <small>${shot.duration}s · ${simpleLabel(shot.status)}</small></button>`).join("") : "暂无镜头";
  document.querySelectorAll("[data-shot]").forEach((button) => button.addEventListener("click", () => selectShot(button.dataset.shot)));
}

async function selectShot(id) {
  state.shot = await request(`/v1/shots/${id}`);
  renderShots();
  const allShots = state.episode.scenes.flatMap((scene) => scene.shots);
  const index = allShots.findIndex((shot) => shot.id === id) + 1;
  $("shotNumber").textContent = `镜头 ${String(index).padStart(2, "0")}`;
  $("shotAction").textContent = state.shot.user_prompt;
  $("shotState").textContent = `${state.shot.input_state ? "开始画面已确定" : "未设定开始画面"} → 一个主要动作 → ${state.shot.output_state ? "结束画面已规划" : "未设定结束画面"}`;
  $("shotTitle").textContent = `${simpleLabel(state.shot.shot_type)} · 镜头 ${index}`;
  $("shotPrompt").textContent = state.shot.user_prompt;
  $("shotDuration").textContent = `${state.shot.duration}s`;
  $("shotContinuity").textContent = simpleLabel(state.shot.continuity_policy);
  $("shotPolicy").textContent = simpleLabel(state.shot.generation_policy);
  $("shotProvider").textContent = simpleLabel(state.shot.provider);
  $("rawPrompt").value = state.shot.user_prompt;
  $("compiledPrompt").value = state.shot.compiled_prompt || "";
  await loadCandidates();
}

async function loadCandidates() {
  if (!state.shot) return;
  state.candidates = await request(`/v1/shots/${state.shot.id}/candidates`);
  $("candidateGrid").classList.toggle("empty-state", !state.candidates.length);
  $("candidateGrid").innerHTML = state.candidates.length ? state.candidates.map((candidate, index) => {
    const qa = candidate.qa || {};
    const score = Math.round((qa.overall_score || 0) * 100);
    const needsHumanReview = candidate.status === "USER_REVIEW_REQUIRED";
    const canCommit = candidate.status === "PASSED";
    const reviewBlocked = ["HARD_FAILED", "REJECTED", "COMMITTED"].includes(candidate.status);
    const humanReview = needsHumanReview && !reviewBlocked ? `
      <section class="human-review-box" aria-label="人工确认">
        <strong>请你亲自确认这个结果</strong>
        <p>自动检查还没有足够信息做决定。请查看人物、动作和画面衔接，再写下确认理由。</p>
        <label>确认理由
          <textarea data-review-reason="${escapeHTML(candidate.id)}" rows="3" placeholder="例如：已核对人物外观、视线和上一镜衔接，结果可用。"></textarea>
        </label>
        <label class="human-review-confirm">
          <input type="checkbox" data-review-confirm="${escapeHTML(candidate.id)}" />
          <span>我已亲自查看结果，并确认它可以进入采用流程</span>
        </label>
        <button class="human-review-button" data-human-review="${escapeHTML(candidate.id)}" disabled>确认通过</button>
        <small>确认成功后，状态变为“检查通过”才能采用。</small>
      </section>` : "";
    const validateAction = needsHumanReview || reviewBlocked ? "" : `<button data-validate="${escapeHTML(candidate.id)}">检查质量</button>`;
    const commitAction = canCommit ? `<button data-commit="${escapeHTML(candidate.id)}">采用</button>` : "";
    return `<article class="candidate"><div class="candidate-head"><span>方案 ${String.fromCharCode(65 + index)}</span><b class="candidate-status">${simpleLabel(candidate.status)}</b></div>
      <div class="score-bars">${[["综合",score],["人物",Math.round((qa.character_score||0)*100)],["摄影",Math.round((qa.camera_score||0)*100)],["动作",Math.round((qa.action_score||0)*100)]].map(([name,value]) => `<div class="score-row"><span>${name}</span><div class="bar"><i style="width:${value}%"></i></div><b>${value}</b></div>`).join("")}</div>
      <div class="decision-box">${escapeHTML(qa.summary || "等待生成或检查")}<br>实际费用 $${candidate.cost.toFixed(2)} · 约 ${Math.max(1, Math.ceil(candidate.cost / .01))} 积分</div>
      ${humanReview}<div class="candidate-actions">${validateAction}${commitAction}</div></article>`;
  }).join("") : "生成后可对比方案 A / B / C、质量检查与成本";
  document.querySelectorAll("[data-validate]").forEach((button) => button.addEventListener("click", () => validateCandidate(button.dataset.validate)));
  document.querySelectorAll("[data-commit]").forEach((button) => button.addEventListener("click", () => commitCandidate(button.dataset.commit)));
  document.querySelectorAll("[data-human-review]").forEach((button) => button.addEventListener("click", () => humanReviewCandidate(button.dataset.humanReview).catch((error) => toast(error.message))));
  document.querySelectorAll("[data-review-reason]").forEach((input) => input.addEventListener("input", () => updateHumanReviewControl(input.dataset.reviewReason)));
  document.querySelectorAll("[data-review-confirm]").forEach((input) => input.addEventListener("change", () => updateHumanReviewControl(input.dataset.reviewConfirm)));
}

function updateHumanReviewControl(candidateId) {
  const reason = document.querySelector(`[data-review-reason="${candidateId}"]`);
  const confirmation = document.querySelector(`[data-review-confirm="${candidateId}"]`);
  const button = document.querySelector(`[data-human-review="${candidateId}"]`);
  if (button) button.disabled = !reason?.value.trim() || !confirmation?.checked;
}

let newProjectReturnFocus = null;
let projectCreationPending = false;

function openNewProjectDialog() {
  const dialog = $("newProjectDialog");
  newProjectReturnFocus = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : $("newProjectBtn");
  $("newProjectName").value = "竖屏短剧计划";
  $("newProjectStatus").textContent = "";
  $("newProjectError").textContent = "";
  $("newProjectName").setAttribute("aria-invalid", "false");
  $("cancelNewProjectBtn").disabled = false;
  $("confirmNewProjectBtn").disabled = false;
  $("confirmNewProjectBtn").textContent = "创建项目";
  if (!dialog.open) dialog.showModal();
  requestAnimationFrame(() => {
    $("newProjectName").focus();
    $("newProjectName").select();
  });
}

function closeNewProjectDialog() {
  const dialog = $("newProjectDialog");
  if (!dialog.open || projectCreationPending) return;
  dialog.close();
}

async function createProject(name) {
  const project = await request("/v1/projects", { method: "POST", body: JSON.stringify({ title: name }) });
  await loadProjects(); await selectProject(project.id); toast("项目已创建");
}

async function submitNewProject(event) {
  event.preventDefault();
  if (projectCreationPending) return;
  const name = $("newProjectName").value.trim();
  $("newProjectError").textContent = "";
  if (!name) {
    $("newProjectError").textContent = "请输入项目名称";
    $("newProjectName").setAttribute("aria-invalid", "true");
    $("newProjectName").focus();
    return;
  }
  projectCreationPending = true;
  $("newProjectStatus").textContent = "正在创建并切换到新项目…";
  $("cancelNewProjectBtn").disabled = true;
  $("confirmNewProjectBtn").disabled = true;
  $("confirmNewProjectBtn").textContent = "正在创建…";
  try {
    await createProject(name);
    projectCreationPending = false;
    $("newProjectDialog").close();
  } catch (error) {
    $("newProjectStatus").textContent = "";
    $("newProjectError").textContent = error.message || "项目创建失败，请稍后重试";
  } finally {
    projectCreationPending = false;
    $("cancelNewProjectBtn").disabled = false;
    $("confirmNewProjectBtn").disabled = false;
    $("confirmNewProjectBtn").textContent = "创建项目";
  }
}

async function compileScript() {
  if (!state.project) { toast("请先创建项目"); return; }
  const script = $("scriptInput").value.trim();
  if (!script) { toast("请输入剧本"); return; }
  let episodeId = state.project.episodes[0]?.id;
  if (!episodeId) {
    const episode = await request(`/v1/projects/${state.project.id}/episodes`, { method: "POST", body: JSON.stringify({ project_id: state.project.id, title: "第一集", episode_number: 1, script_source: script }) });
    episodeId = episode.id;
  } else if (state.episode?.script_source !== script) {
    toast("为了保护已有镜头，系统不会直接覆盖当前剧本；请新建项目后再次拆解。"); return;
  }
  await request(`/v1/episodes/${episodeId}/compile`, { method: "POST", body: "{}" });
  await selectProject(state.project.id); toast("剧本已拆解为场景和镜头，前后衔接信息也已保存");
}

async function generateShot() {
  if (!state.shot) { toast("请先选择镜头"); return; }
  const projectId = state.project.id;
  const shotId = state.shot.id;
  const estimatedCost = Number($("estimatedCost").value || 0);
  const fingerprint = JSON.stringify({ projectId, shotId, estimatedCost });
  const idempotencyKey = beginSubmission("shot", fingerprint);
  if (!idempotencyKey) return;
  const button = $("generateBtn");
  button.disabled = true;
  button.textContent = "正在提交…";
  let succeeded = false;
  try {
    await request(`/v1/shots/${shotId}/generate`, {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idempotencyKey, estimated_cost: estimatedCost }),
    });
    await loadCandidates();
    succeeded = true;
    toast("备选方案已排队；网络重试会复用同一任务，不会重复提交");
  } finally {
    finishSubmission("shot", idempotencyKey, succeeded);
    button.disabled = false;
    button.textContent = "生成 / 重做当前镜头";
  }
}

async function validateCandidate(id) {
  await request(`/v1/shots/${state.shot.id}/candidates/${id}/validate`, { method: "POST", body: JSON.stringify({ evidence: {} }) });
  await loadCandidates(); toast("基础质量检查已执行；视觉证据不足时会要求人工复核");
}

async function humanReviewCandidate(id) {
  const candidate = state.candidates.find((item) => item.id === id);
  if (!candidate || candidate.status !== "USER_REVIEW_REQUIRED") {
    await loadCandidates();
    return toast("该方案状态已更新，请按当前状态继续操作");
  }
  const reason = document.querySelector(`[data-review-reason="${id}"]`)?.value.trim() || "";
  const explicitConfirmation = document.querySelector(`[data-review-confirm="${id}"]`)?.checked === true;
  if (!reason) return toast("请填写你确认通过的理由");
  if (!explicitConfirmation) return toast("请勾选明确确认");
  const button = document.querySelector(`[data-human-review="${id}"]`);
  if (button) {
    button.disabled = true;
    button.textContent = "正在提交确认…";
  }
  try {
    await request(`/v1/shots/${state.shot.id}/candidates/${id}/human-review`, {
      method: "POST",
      body: JSON.stringify({ reason, explicit_confirmation: true }),
    });
    await loadCandidates();
    toast("人工确认已记录；该方案现在可以采用");
  } catch (error) {
    if (button) {
      button.textContent = "确认通过";
      updateHumanReviewControl(id);
    }
    throw error;
  }
}

async function commitCandidate(id) {
  await request(`/v1/shots/${state.shot.id}/candidates/${id}/commit`, { method: "POST", body: "{}" });
  await selectShot(state.shot.id); toast("备选方案已通过检查并写入时间线");
}

async function createCharacter() {
  if (!state.project) return;
  const name = $("characterName").value.trim(); if (!name) return toast("请输入角色名称");
  const character = await request("/v1/characters", { method: "POST", body: JSON.stringify({ project_id: state.project.id, name, description: $("characterDescription").value }) });
  state.selectedCharacterId = character.id;
  await loadCharacters(); toast("角色档案已建立；上传主参考图后可设为当前正式参考");
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
    const identity = latest ? `身份 v${latest.version} 已锁定 · 可上传新图创建 v${latest.version + 1}` : "尚未锁定主参考图";
    return `<button class="binding character-binding${selected}" data-character="${character.id}"><strong>${escapeHTML(character.name)}</strong><span>${identity}</span></button>`;
  }).join("") : "<p>尚未建立角色</p>";
  document.querySelectorAll("[data-character]").forEach((button) => button.addEventListener("click", () => {
    state.selectedCharacterId = button.dataset.character; renderCharacters();
  }));
}

async function confirmCharacterIdentity() {
  if (!state.project || !state.selectedCharacterId) return toast("请先建立并选择角色");
  const file = $("characterAsset").files[0];
  if (!file) return toast("请选择一张人物参考图");
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
    throw new Error(detail.detail || "人物图片上传失败");
  }
  const asset = await upload.json();
  const identity = await request(`/v1/characters/${state.selectedCharacterId}/confirm-identity`, {
    method: "POST", body: JSON.stringify({ master_asset_id: asset.id }),
  });
  $("characterAsset").value = "";
  await loadCharacters();
  toast(`人物身份 v${identity.version} 已锁定；旧版本仍保留`);
}

async function continuity() {
  if (!state.shot) return;
  const isReverse = $("cameraAngle").value === "侧面" && $("cameraMove").value === "环绕";
  const result = await request(`/v1/shots/${state.shot.id}/continuity`, { method: "POST", body: JSON.stringify({ project_id: state.project.id, risk: { camera_axis_delta: isReverse ? .8 : .12, camera_angle_delta: isReverse ? .7 : .15, action_continuity: .9, previous_frame_quality: .85 } }) });
  const reasons = result.reasons.map((reason) => simpleLabel(reason)).join(" · ");
  $("continuityResult").textContent = `${simpleLabel(result.mode)} · 衔接风险 ${Math.round(result.risk_score*100)} / 100 · ${reasons}`;
}

document.querySelectorAll(".inspector-tabs button").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".inspector-tabs button,.tab-panel").forEach((item) => item.classList.remove("active"));
  button.classList.add("active"); $(`tab-${button.dataset.tab}`).classList.add("active");
}));

$("newProjectBtn").addEventListener("click", openNewProjectDialog);
$("newProjectForm").addEventListener("submit", submitNewProject);
$("cancelNewProjectBtn").addEventListener("click", closeNewProjectDialog);
$("newProjectName").addEventListener("input", () => {
  $("newProjectError").textContent = "";
  $("newProjectStatus").textContent = "";
  $("newProjectName").setAttribute("aria-invalid", "false");
});
$("newProjectDialog").addEventListener("cancel", (event) => {
  if (projectCreationPending) event.preventDefault();
});
$("newProjectDialog").addEventListener("close", () => {
  const returnFocus = newProjectReturnFocus;
  newProjectReturnFocus = null;
  if (returnFocus?.isConnected) requestAnimationFrame(() => returnFocus.focus());
});
$("newProjectDialog").addEventListener("click", (event) => {
  if (event.target === $("newProjectDialog")) closeNewProjectDialog();
});
$("compileBtn").addEventListener("click", () => compileScript().catch((error) => toast(error.message)));
$("generateBtn").addEventListener("click", () => generateShot().catch((error) => toast(error.message)));
$("refreshCandidatesBtn").addEventListener("click", () => loadCandidates().catch((error) => toast(error.message)));
$("createCharacterBtn").addEventListener("click", () => createCharacter().catch((error) => toast(error.message)));
$("confirmCharacterBtn").addEventListener("click", () => confirmCharacterIdentity().catch((error) => toast(error.message)));
$("continuityBtn").addEventListener("click", () => continuity().catch((error) => toast(error.message)));
$("projectSelect").addEventListener("change", (event) => selectProject(event.target.value).catch((error) => toast(error.message)));
$("authForm").addEventListener("submit", submitAuth);
$("authModeBtn").addEventListener("click", () => setAuthMode(state.authMode === "login" ? "register" : "login"));
$("logoutBtn").addEventListener("click", logout);

document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => switchMode(button.dataset.mode)));
document.querySelectorAll("[data-media]").forEach((button) => button.addEventListener("click", () => setPassengerMedia(button.dataset.media)));
$("correctImagePromptBtn").addEventListener("click", () => correctPassengerPrompt().catch((error) => toast(error.message)));
$("undoImagePromptBtn").addEventListener("click", undoPassengerPrompt);
$("passengerGenerateBtn").addEventListener("click", () => generatePassenger().catch((error) => toast(error.message)));
$("passengerRefreshBtn").addEventListener("click", () => refreshPassengerJob().catch((error) => toast(error.message)));
$("promotePassengerAssetBtn").addEventListener("click", () => confirmPassengerAsset().catch((error) => toast(error.message)));
$("manualExistingAsset").addEventListener("change", syncManualAssetSelection);
$("manualAssetUploadBtn").addEventListener("click", () => uploadManualAssetVersion().catch((error) => toast(error.message)));
$("lockProjectStyleBtn").addEventListener("click", () => lockSelectedProjectStyle().catch((error) => toast(error.message)));
$("passengerModel").addEventListener("change", updatePassengerCost);
$("passengerDuration").addEventListener("input", updatePassengerCost);
$("passengerResolution").addEventListener("change", updatePassengerCost);
$("passengerReference").addEventListener("change", () => {
  state.passengerReferenceUpload = null;
  updatePassengerCost();
});

switchMode("passenger"); setPassengerMedia("image");
setAuthMode("login"); bootstrapAuth().catch((error) => toast(error.message));
