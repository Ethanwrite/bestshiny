/*
 * Browser transport adapted from FlowKit's MIT extension architecture.
 * Unlike the reference implementation this worker never auto-solves or bypasses
 * interactive verification. A user must explicitly authorize each captcha-backed request.
 */
const DEFAULTS = {
  serverBase: 'http://127.0.0.1:8080', apiKey: '', workerId: '', accountId: '',
  provider: 'google_flow', maxJobs: 1,
};

let settings = {...DEFAULTS};
let connectionId = '';
let bearerToken = '';
let observedApiKey = '';
let pendingCaptchaToken = '';
let busy = false;

function randomId(prefix) {
  return `${prefix}-${crypto.randomUUID()}`;
}

async function loadSettings() {
  settings = {...DEFAULTS, ...(await chrome.storage.local.get(DEFAULTS))};
  if (!settings.workerId) {
    settings.workerId = randomId('flow-worker');
    await chrome.storage.local.set({workerId: settings.workerId});
  }
  connectionId = randomId('connection');
}

function headers() {
  const result = {'Content-Type': 'application/json'};
  if (settings.apiKey) result.Authorization = `Bearer ${settings.apiKey}`;
  return result;
}

async function api(path, options = {}) {
  const response = await fetch(`${settings.serverBase.replace(/\/$/, '')}${path}`, {
    ...options, headers: {...headers(), ...(options.headers || {})},
  });
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = {detail: text}; }
  if (!response.ok) throw new Error(data.detail || `HTTP_${response.status}`);
  return data;
}

async function register() {
  return api('/v1/workers/register', {method: 'POST', body: JSON.stringify({
    worker_id: settings.workerId, provider: settings.provider,
    account_id: settings.accountId || null, connection_id: connectionId,
    capabilities: ['image', 'video', 'upload', 'poll'], max_jobs: Number(settings.maxJobs) || 1,
    metadata: {extension_version: chrome.runtime.getManifest().version, flow_key_present: !!bearerToken},
  })});
}

async function heartbeat(status = null) {
  const state = status || (busy ? 'BUSY' : 'READY');
  return api(`/v1/workers/${encodeURIComponent(settings.workerId)}/heartbeat`, {
    method: 'POST', body: JSON.stringify({
      connection_id: connectionId, status: state, current_jobs: busy ? 1 : 0,
      metadata: {flow_key_present: !!bearerToken, user_authorization_ready: !!pendingCaptchaToken},
    }),
  });
}

async function respond(commandId, response = null, error = null) {
  await api(`/v1/workers/${encodeURIComponent(settings.workerId)}/responses`, {
    method: 'POST', body: JSON.stringify({connection_id: connectionId, command_id: commandId, response, error}),
  });
}

function providerUrl(rawUrl) {
  const url = new URL(rawUrl);
  if (url.origin !== 'https://aisandbox-pa.googleapis.com') throw new Error('INVALID_PROVIDER_URL');
  if (!url.searchParams.get('key') && observedApiKey) url.searchParams.set('key', observedApiKey);
  return url.toString();
}

async function providerRequest(command) {
  const payload = command.payload || {};
  if (payload.provider !== 'google_flow') throw new Error('UNSUPPORTED_PROVIDER');
  if (!bearerToken) throw new Error('WORKER_NEEDS_USER_ACTION: open Google Flow while signed in');
  const needsAuthorization = !!payload.captcha_action;
  if (needsAuthorization && !pendingCaptchaToken) {
    await heartbeat('NEEDS_USER_ACTION');
    throw new Error('WORKER_NEEDS_USER_ACTION: click “Authorize next generation” in the extension');
  }
  const body = structuredClone(payload.body || {});
  if (needsAuthorization) {
    if (body.clientContext?.recaptchaContext) body.clientContext.recaptchaContext.token = pendingCaptchaToken;
    for (const request of body.requests || []) {
      if (request.clientContext?.recaptchaContext) request.clientContext.recaptchaContext.token = pendingCaptchaToken;
    }
    pendingCaptchaToken = '';
    await chrome.storage.local.remove('pendingCaptchaToken');
  }
  const response = await fetch(providerUrl(payload.url), {
    method: payload.method || 'POST', credentials: 'include',
    headers: {...(payload.headers || {}), Authorization: `Bearer ${bearerToken}`},
    body: (payload.method || 'POST') === 'GET' ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let data;
  try { data = JSON.parse(text); } catch { data = text; }
  if (response.status === 401) {
    bearerToken = '';
    await chrome.storage.local.remove('bearerToken');
  }
  return {status: response.status, data};
}

async function mediaUrl(command) {
  if (command.payload?.provider !== 'google_flow') throw new Error('UNSUPPORTED_PROVIDER');
  const mediaId = command.payload.media_id;
  if (!mediaId) throw new Error('MISSING_MEDIA_ID');
  const url = new URL('https://labs.google/fx/api/trpc/media.getMediaUrlRedirect');
  url.searchParams.set('name', mediaId);
  const response = await fetch(url, {credentials: 'include', redirect: 'follow'});
  if (!response.ok) throw new Error(`MEDIA_URL_HTTP_${response.status}`);
  await response.body?.cancel();
  return {url: response.url};
}

async function processCommand(command) {
  busy = true;
  await heartbeat();
  try {
    let response;
    if (command.type === 'provider.request') response = await providerRequest(command);
    else if (command.type === 'provider.media_url') response = await mediaUrl(command);
    else throw new Error(`UNKNOWN_COMMAND:${command.type}`);
    await respond(command.id, response, null);
  } catch (error) {
    await respond(command.id, null, error.message || String(error));
  } finally {
    busy = false;
    await heartbeat().catch(() => {});
  }
}

async function poll() {
  if (busy) return;
  try {
    const data = await api(`/v1/workers/${encodeURIComponent(settings.workerId)}/commands?connection_id=${encodeURIComponent(connectionId)}`);
    for (const command of data.commands || []) await processCommand(command);
  } catch (error) {
    if (String(error).includes('stale') || String(error).includes('missing')) await register();
  }
}

async function initialize() {
  await loadSettings();
  const saved = await chrome.storage.local.get(['bearerToken', 'pendingCaptchaToken']);
  bearerToken = saved.bearerToken || '';
  pendingCaptchaToken = saved.pendingCaptchaToken || '';
  await register().catch(() => {});
  chrome.alarms.create('worker-poll', {periodInMinutes: 0.05});
  chrome.alarms.create('worker-heartbeat', {periodInMinutes: 0.25});
}

chrome.runtime.onInstalled.addListener(initialize);
chrome.runtime.onStartup.addListener(initialize);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'worker-poll') poll();
  if (alarm.name === 'worker-heartbeat') heartbeat().catch(() => register().catch(() => {}));
});

chrome.webRequest.onBeforeSendHeaders.addListener((details) => {
  const authorization = (details.requestHeaders || []).find((header) => header.name.toLowerCase() === 'authorization');
  if (authorization?.value?.startsWith('Bearer ')) {
    bearerToken = authorization.value.slice(7);
    chrome.storage.local.set({bearerToken});
  }
  try {
    const key = new URL(details.url).searchParams.get('key');
    if (key) observedApiKey = key;
  } catch {}
}, {urls: ['https://aisandbox-pa.googleapis.com/*']}, ['requestHeaders', 'extraHeaders']);

chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  if (message.type === 'SETTINGS_UPDATED') {
    loadSettings().then(register).then(() => reply({ok: true})).catch((error) => reply({error: error.message}));
    return true;
  }
  if (message.type === 'AUTHORIZE_NEXT_GENERATION') {
    authorizeNextGeneration().then(reply).catch((error) => reply({error: error.message}));
    return true;
  }
  if (message.type === 'STATUS') {
    reply({workerId: settings.workerId, connectionId, connected: !!connectionId,
      flowKeyPresent: !!bearerToken, authorizationReady: !!pendingCaptchaToken, busy});
  }
});

async function authorizeNextGeneration() {
  const tabs = await chrome.tabs.query({url: ['https://labs.google/fx/tools/flow*', 'https://labs.google/fx/*/tools/flow*']});
  if (!tabs.length) throw new Error('Open Google Flow and sign in first');
  const response = await chrome.tabs.sendMessage(tabs[0].id, {
    type: 'GET_CAPTCHA_WITH_USER_ACTION', requestId: randomId('auth'), pageAction: 'VIDEO_GENERATION',
  });
  if (!response?.token) throw new Error(response?.error || 'Authorization failed');
  pendingCaptchaToken = response.token;
  await chrome.storage.local.set({pendingCaptchaToken});
  await heartbeat('READY');
  return {ok: true};
}

initialize().catch(() => {});

