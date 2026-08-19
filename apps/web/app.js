const API = window.AI_DIRECTOR_API || (location.port === "3000" ? "/api" : "http://127.0.0.1:18080");
const state = { projects: [], project: null, episode: null, shot: null, candidates: [], characters: [], selectedCharacterId: null };
const $ = (id) => document.getElementById(id);
const escapeHTML = (value = "") => String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
const simpleLabel = (value) => ({
  DRAFT: "草稿", PLANNED: "已规划", READY: "可生成", QUEUED: "排队中", GENERATING: "生成中",
  VALIDATING: "检查中", PASSED: "检查通过", SOFT_FAILED: "建议修复", HARD_FAILED: "未通过",
  USER_REVIEW_REQUIRED: "需要人工确认", COMMITTED: "已采用", REJECTED: "未采用", FAILED: "失败",
  HARD_CONTINUITY: "紧接上一镜", HYBRID: "尾帧加参考图", RE_ANCHOR: "重新固定人物与场景",
  TEXT_TO_VIDEO: "文字生成视频", IMAGE_TO_VIDEO: "图片生成视频", CONTINUE_I2V: "沿用上一镜继续生成",
  START_END_FRAME: "指定首尾画面", google_flow: "Google Flow", seedance: "Seedance",
  veo_official: "Veo", grok: "Grok", kling: "可灵", runway: "Runway", omni: "Omni",
}[value] || value || "—");

async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(detail.detail || `请求失败 (${response.status})`);
  }
  return response.json();
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

async function loadProjects() {
  state.projects = await request("/v1/projects");
  $("projectSelect").innerHTML = state.projects.length
    ? state.projects.map((project) => `<option value="${project.id}">${escapeHTML(project.name)}</option>`).join("")
    : '<option value="">尚无项目</option>';
  if (state.projects.length) await selectProject(state.projects[0].id);
}

async function selectProject(id) {
  if (!id) return;
  state.project = await request(`/v1/projects/${id}`);
  $("projectSelect").value = id;
  await loadCharacters();
  if (state.project.episodes.length) await loadEpisode(state.project.episodes[0].id);
  else resetProductionView();
}

function resetProductionView() {
  state.episode = null; state.shot = null; state.candidates = [];
  $("sceneList").innerHTML = "创建第一集并编译剧本后显示场景";
  $("shotTimeline").innerHTML = "暂无镜头";
  $("candidateGrid").innerHTML = "生成后可对比方案 A / B / C、质量检查与成本";
}

async function loadEpisode(id) {
  state.episode = await request(`/v1/episodes/${id}`);
  $("scriptInput").value = state.episode.script_source || "";
  $("episodeStatus").textContent = state.episode.status;
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
  $("shotState").textContent = `${state.shot.input_state ? "输入已锁定" : "无输入状态"} → 单一动作 → ${state.shot.output_state ? "输出已规划" : "无输出状态"}`;
  $("shotTitle").textContent = `${state.shot.shot_type} · 镜头 ${index}`;
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
    return `<article class="candidate"><div class="candidate-head"><span>方案 ${String.fromCharCode(65 + index)}</span><b class="candidate-status">${simpleLabel(candidate.status)}</b></div>
      <div class="score-bars">${[["综合",score],["人物",Math.round((qa.character_score||0)*100)],["摄影",Math.round((qa.camera_score||0)*100)],["动作",Math.round((qa.action_score||0)*100)]].map(([name,value]) => `<div class="score-row"><span>${name}</span><div class="bar"><i style="width:${value}%"></i></div><b>${value}</b></div>`).join("")}</div>
      <div class="decision-box">${escapeHTML(qa.summary || "等待生成或检查")}<br>实际成本 ${candidate.cost.toFixed(2)}</div>
      <div class="candidate-actions"><button data-validate="${candidate.id}">检查质量</button><button data-commit="${candidate.id}" ${qa.decision !== "PASS" ? "disabled" : ""}>采用</button></div></article>`;
  }).join("") : "生成后可对比方案 A / B / C、质量检查与成本";
  document.querySelectorAll("[data-validate]").forEach((button) => button.addEventListener("click", () => validateCandidate(button.dataset.validate)));
  document.querySelectorAll("[data-commit]").forEach((button) => button.addEventListener("click", () => commitCandidate(button.dataset.commit)));
}

async function createProject() {
  const name = prompt("项目名称", "竖屏短剧计划");
  if (!name) return;
  const project = await request("/v1/projects", { method: "POST", body: JSON.stringify({ title: name }) });
  await loadProjects(); await selectProject(project.id); toast("项目已创建");
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
    toast("当前 V1 为保护已生成状态，不会静默覆盖已存在剧本；请新建项目重新编译。"); return;
  }
  await request(`/v1/episodes/${episodeId}/compile`, { method: "POST", body: "{}" });
  await selectProject(state.project.id); toast("剧本已编译为场景、事件、镜头和状态链");
}

async function generateShot() {
  if (!state.shot) { toast("请先选择镜头"); return; }
  const key = `${state.project.id}-${state.shot.id}-${Date.now()}`;
  await request(`/v1/shots/${state.shot.id}/generate`, { method: "POST", body: JSON.stringify({ idempotency_key: key, estimated_cost: Number($("estimatedCost").value || 0) }) });
  await loadCandidates(); toast("备选方案已排队；重复扣费保护已启用");
}

async function validateCandidate(id) {
  await request(`/v1/shots/${state.shot.id}/candidates/${id}/validate`, { method: "POST", body: JSON.stringify({ evidence: {} }) });
  await loadCandidates(); toast("基础质量检查已执行；视觉证据不足时会要求人工复核");
}

async function commitCandidate(id) {
  await request(`/v1/shots/${state.shot.id}/candidates/${id}/commit`, { method: "POST", body: "{}" });
  await selectShot(state.shot.id); toast("备选方案已通过检查并写入时间线");
}

async function refinePrompt() {
  if (!state.project) return;
  const result = await request("/v1/prompts/refine", { method: "POST", body: JSON.stringify({ project_id: state.project.id, prompt: $("rawPrompt").value || $("scriptInput").value }) });
  $("compiledPrompt").value = result.refined;
  $("promptDiff").textContent = result.changes.length ? `${result.changes.length} 处表达整理；剧情事实保持不变。` : "表达已经清晰，没有修改。";
}

async function createCharacter() {
  if (!state.project) return;
  const name = $("characterName").value.trim(); if (!name) return toast("请输入角色名称");
  const character = await request("/v1/characters", { method: "POST", body: JSON.stringify({ project_id: state.project.id, name, description: $("characterDescription").value }) });
  state.selectedCharacterId = character.id;
  await loadCharacters(); toast("角色档案已建立；上传主参考图后可锁定身份版本");
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
  const upload = await fetch(`${API}/v1/assets`, { method: "POST", body: form });
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
  $("continuityResult").textContent = `${result.mode} · 风险 ${Math.round(result.risk_score*100)} · ${result.reasons.join(" / ")}`;
}

document.querySelectorAll(".inspector-tabs button").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".inspector-tabs button,.tab-panel").forEach((item) => item.classList.remove("active"));
  button.classList.add("active"); $(`tab-${button.dataset.tab}`).classList.add("active");
}));

$("newProjectBtn").addEventListener("click", () => createProject().catch((error) => toast(error.message)));
$("compileBtn").addEventListener("click", () => compileScript().catch((error) => toast(error.message)));
$("generateBtn").addEventListener("click", () => generateShot().catch((error) => toast(error.message)));
$("refreshCandidatesBtn").addEventListener("click", () => loadCandidates().catch((error) => toast(error.message)));
$("promptRefineBtn").addEventListener("click", () => refinePrompt().catch((error) => toast(error.message)));
$("refineBtn").addEventListener("click", () => { document.querySelector('[data-tab="prompt"]').click(); $("rawPrompt").value = $("scriptInput").value; });
$("createCharacterBtn").addEventListener("click", () => createCharacter().catch((error) => toast(error.message)));
$("confirmCharacterBtn").addEventListener("click", () => confirmCharacterIdentity().catch((error) => toast(error.message)));
$("continuityBtn").addEventListener("click", () => continuity().catch((error) => toast(error.message)));
$("projectSelect").addEventListener("change", (event) => selectProject(event.target.value).catch((error) => toast(error.message)));

health(); loadProjects().catch((error) => toast(error.message));
