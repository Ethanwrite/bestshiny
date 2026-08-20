const fields = ['serverBase', 'workerToken', 'accountId', 'workerId'];

async function refresh() {
  const saved = await chrome.storage.local.get(fields);
  for (const field of fields) document.getElementById(field).value = saved[field] || (field === 'serverBase' ? 'http://127.0.0.1:8080' : '');
  const status = await chrome.runtime.sendMessage({type: 'STATUS'});
  document.getElementById('status').textContent = `Worker：${status.workerId || '未设置'}\n登录凭据：${status.flowKeyPresent ? '已捕获' : '请打开 Flow'}\n下次生成授权：${status.authorizationReady ? '已准备' : '未准备'}`;
}

document.getElementById('save').onclick = async () => {
  const values = Object.fromEntries(fields.map((field) => [field, document.getElementById(field).value.trim()]));
  await chrome.storage.local.set(values);
  await chrome.runtime.sendMessage({type: 'SETTINGS_UPDATED'});
  await refresh();
};

document.getElementById('authorize').onclick = async () => {
  const result = await chrome.runtime.sendMessage({type: 'AUTHORIZE_NEXT_GENERATION'});
  if (result.error) alert(result.error);
  await refresh();
};

refresh();
